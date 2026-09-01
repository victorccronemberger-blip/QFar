"""
minute_api.py — Cliente da minute-api (app Minute, com.bakerdata.minute).

Biblioteca (sem efeitos de CLI). A interface de linha de comando fica em
`scripts/minute_cli.py`.

Cobre autenticação (Firebase Identity Toolkit), sessão com refresh automático de
token e chamadas à API documentadas em `reference/openapi.json` (98 endpoints).
Somente stdlib.

Exemplo:
    from moneymin.minute_api import login, Session
    login("user@example.com", "senha")          # grava secrets/token_user_at_example_com.json
    s = Session.from_email("user@example.com")   # carrega + SEMPRE troca o idToken no Firebase
    me = s.get("/api/v1/users/me")
    cats = s.get("/api/v1/categories")
"""
from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from . import config, device_profile, transport
from .atomic_io import save_json

# Folga antes do exp do JWT: PUT de blob pode passar de 2 min; 10 min evita
# mandar um Bearer que morre no meio do upload.
_REFRESH_SKEW_S = 10 * 60
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

# --- Endpoints Firebase / Google Identity Toolkit ---------------------------
_SIGNIN_URL = (
    "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
    f"?key={config.FIREBASE_API_KEY}"
)
_REFRESH_URL = f"https://securetoken.googleapis.com/v1/token?key={config.FIREBASE_API_KEY}"


class AuthError(RuntimeError):
    """Falha de autenticação (token ausente, expirado sem refresh válido, etc.)."""


@dataclass(frozen=True)
class HttpResponse:
    """Resposta HTTP completa para decisões de retry e bloqueio."""

    status: int
    text: str
    headers: dict[str, str]


def _as_list(body: Any, keys: tuple[str, ...] = ("tasks", "items", "data", "results")) -> list:
    """Normaliza respostas que às vezes vêm como lista e às vezes como objeto.

    Aceita o texto cru da resposta, um dict ou uma lista e devolve sempre uma
    lista (vazia se não houver nada utilizável).
    """
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return []
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in keys:
            if isinstance(body.get(key), list):
                return body[key]
    return []


# --- HTTP baixo nível --------------------------------------------------------
def _request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: Any = None,
    timeout: int = 30,
    raw_form: bool = False,
) -> tuple[int, str]:
    """Executa uma requisição HTTP e devolve (status, corpo_texto).

    O transporte (TLS/HTTP fingerprint) vem de `transport.py`: curl_cffi com
    impersonate iOS se instalado, senão urllib stdlib. Nunca levanta exceção
    de rede: em erro devolve (-1, "ERRO: ...").
    """
    headers = dict(headers or {})
    if isinstance(body, (dict, list)):
        data = json.dumps(body).encode()
    elif isinstance(body, str):
        data = body.encode()
    else:
        data = None

    if data and not raw_form and not any(k.lower() == "content-type" for k in headers):
        headers["Content-Type"] = "application/json"

    try:
        status, raw = transport.http_request(method, url, headers=headers,
                                             body=data, timeout=timeout)
        return status, raw.decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 — cliente devolve erro em vez de propagar
        return -1, f"ERRO: {exc}"


def _request_detailed(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: Any = None,
    timeout: int = 30,
) -> HttpResponse:
    """Executa HTTP preservando headers como ``X-Blocked-Reason``."""
    request_headers = dict(headers or {})
    if isinstance(body, (dict, list)):
        data = json.dumps(body).encode()
    elif isinstance(body, str):
        data = body.encode()
    else:
        data = None
    if data and not any(
            key.casefold() == "content-type" for key in request_headers):
        request_headers["Content-Type"] = "application/json"
    try:
        status, raw, response_headers = transport.http_request_detailed(
            method, url, headers=request_headers, body=data, timeout=timeout)
        return HttpResponse(
            int(status), raw.decode("utf-8", "replace"),
            {str(k): str(v) for k, v in response_headers.items()},
        )
    except Exception as exc:  # noqa: BLE001 — mesmo contrato de _request
        return HttpResponse(-1, f"ERRO: {exc}", {})


# --- Autenticação ------------------------------------------------------------
def login(email: str, password: str) -> dict[str, Any]:
    """Autentica no Firebase e grava `secrets/token_<email>.json`.

    Devolve o dict do token. Levanta RuntimeError se o login falhar.
    """
    status, body = _request(
        _SIGNIN_URL,
        "POST",
        body={"email": email, "password": password, "returnSecureToken": True},
    )
    if status != 200:
        raise RuntimeError(f"login falhou ({status}): {body[:300]}")
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"login devolveu resposta inválida: {body[:300]}") from exc

    data["expires_at"] = _expiry_from_token(
        data.get("idToken"), int(data.get("expiresIn", "3600"))
    )
    path = config.token_path(email)
    save_json(path, data)
    return data


def register(email: str, password: str, code: str = config.INVITE_CODE) -> dict[str, Any]:
    """Registra uma nova conta via /auth/web-register e já faz login.

    O endpoint cria o usuário no Firebase, grava no banco e entra na org do
    código de convite — tudo no servidor. Em seguida autentica com a senha
    para gravar `secrets/token_<email>.json`, igual ao comando `login`.

    Usa os headers de identidade do perfil da conta (X-Device-Id + UA com o
    iOS dela) — a conta já nasce associada a UM aparelho.

    Devolve o dict do token. Levanta RuntimeError se o registro falhar.
    """
    headers = device_profile.get_profile(email).headers()
    status, body = _request(
        config.BASE_URL + "/api/v1/auth/web-register",
        "POST",
        headers=headers,
        body={"email": email, "password": password, "code": code},
    )
    if status not in (200, 201):
        raise RuntimeError(f"registro falhou ({status}): {body[:300]}")
    return login(email, password)


def _jwt_exp(token: str | None) -> int:
    """`exp` do JWT (epoch s), sem verificar assinatura. 0 se não for JWT."""
    if not token or token.count(".") < 2:
        return 0
    try:
        payload = token.split(".")[1]
        payload += "=" * ((-len(payload)) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return int(data.get("exp") or 0)
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeError):
        return 0


def _expiry_from_token(id_token: str | None, expires_in: int = 3600) -> int:
    """Prefere o `exp` do JWT (relógio do Firebase) ao relógio local + expiresIn."""
    jwt_exp = _jwt_exp(id_token)
    if jwt_exp > 0:
        return jwt_exp
    return int(time.time()) + int(expires_in)


def _file_expires_at(data: dict[str, Any]) -> int:
    """expires_at do arquivo em epoch s. Aceita milissegundos por engano."""
    raw = data.get("expires_at") or 0
    try:
        ts = int(float(raw))
    except (TypeError, ValueError):
        return 0
    if ts > 10_000_000_000:  # milissegundos
        ts //= 1000
    return ts


def _token_expiry(data: dict[str, Any]) -> int:
    """Epoch em segundos. 0 = desconhecido (tratar como vencido).

    Fonte de verdade: o MENOR entre `expires_at` do arquivo e o `exp` do JWT.
    O arquivo usa relógio local + expiresIn (~21s de skew vs Firebase). O
    Minute valida o JWT no relógio do Google — JWT morto com expires_at no
    futuro era o 401 `Invalid Firebase ID token`.
    """
    file_exp = _file_expires_at(data)
    token = data.get("idToken") or data.get("id_token") or ""
    jwt_exp = _jwt_exp(token)
    candidates = [t for t in (file_exp, jwt_exp) if t > 0]
    if not candidates:
        return 0
    return min(candidates)


def _lock_for(path: Path | None) -> threading.Lock:
    """Um lock por arquivo de token — o refreshToken do Firebase rotaciona."""
    key = str(path.resolve()) if path else ""
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def _lookup_password(email: str | None) -> str | None:
    """Senha salva da conta (contas.jsonl + crowtado_passwords.json)."""
    if not email or "@" not in email:
        return None
    found: str | None = None
    contas = config.DATA_DIR / "contas.jsonl"
    try:
        for line in contas.read_text(encoding="utf-8-sig").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("email") == email and rec.get("senha"):
                found = str(rec["senha"])
    except OSError:
        pass
    pw_path = config.SECRETS_DIR / "crowtado_passwords.json"
    try:
        creds = json.loads(pw_path.read_text(encoding="utf-8-sig"))
        if isinstance(creds, dict) and creds.get(email):
            found = str(creds[email])
    except (OSError, json.JSONDecodeError):
        pass
    return found or None


def _refresh(token_data: dict[str, Any]) -> dict[str, Any]:
    """Renova o idToken usando o refreshToken. Devolve o dict atualizado.

    Levanta AuthError se não houver refreshToken ou se o Firebase recusar
    (refresh token expirado/revogado) — nesse caso é preciso refazer o login.
    """
    refresh_token = token_data.get("refreshToken") or token_data.get("refresh_token")
    if not refresh_token:
        raise AuthError("sem refreshToken no arquivo — refaça o login (comando 'login').")
    status, body = _request(
        _REFRESH_URL,
        "POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }),
        raw_form=True,
    )
    if status != 200:
        raise AuthError(
            f"refresh do token falhou ({status}) — refaça o login (comando 'login'). {body[:200]}"
        )
    try:
        resp = json.loads(body) if (body or "").strip() else None
    except json.JSONDecodeError as exc:
        raise AuthError(
            f"refresh devolveu resposta vazia/não-JSON ({status})."
        ) from exc
    if not isinstance(resp, dict) or not resp.get("id_token"):
        raise AuthError("refresh devolveu resposta vazia/não-JSON.")
    token_data["idToken"] = resp["id_token"]
    token_data["refreshToken"] = resp["refresh_token"]
    token_data["expiresIn"] = str(resp.get("expires_in", "3600"))
    token_data["expires_at"] = _expiry_from_token(
        resp["id_token"], int(resp.get("expires_in", "3600"))
    )
    return token_data


# --- Sessão ------------------------------------------------------------------
class Session:
    """Sessão autenticada com refresh automático e persistência do token."""

    def __init__(self, token_data: dict[str, Any], token_file: Path | None = None,
                 email: str | None = None):
        if "idToken" not in token_data and "id_token" in token_data:
            token_data["idToken"] = token_data["id_token"]
        self.data = token_data
        self.token_file = Path(token_file) if token_file else None
        # e-mail da conta (para o perfil de aparelho: X-Device-Id, UA, uptime)
        self.email = email or token_data.get("email") or None
        # False até um refresh/login nesta instância — o idToken do disco
        # nunca é enviado à API sem troca no Firebase.
        self._live = False
        self._lock = threading.RLock()

    @classmethod
    def from_file(cls, token_file: str | Path, *, live: bool = True) -> Session:
        """Carrega o JSON e, por padrão, troca o idToken no Firebase na hora.

        O arquivo só guarda o refreshToken de forma durável. Mandar o idToken
        estático do disco era o 401 `Invalid Firebase ID token` (exp do JWT
        já morto, token revogado, expires_at em ms). `live=False` só para
        testes que não falam com a rede.
        """
        path = Path(token_file)
        try:
            raw = path.read_text(encoding="utf-8-sig").strip()
            data = json.loads(raw) if raw else None
        except (OSError, json.JSONDecodeError) as exc:
            raise AuthError(
                f"token ilegível em {path.name} — refaça o login."
            ) from exc
        if not isinstance(data, dict):
            raise AuthError(
                f"token vazio ou corrompido em {path.name} — refaça o login."
            )
        sess = cls(data, path)
        if live:
            sess.refresh()
        return sess

    @classmethod
    def from_email(cls, email: str, *, live: bool = True) -> Session:
        path = config.token_path(email)
        if not path.exists():
            raise AuthError(
                f"nenhum token para {email} em {path.name} (pasta secrets/) — rode primeiro: "
                f"python3 scripts/minute_cli.py login {email} <senha>"
            )
        sess = cls.from_file(path, live=False).with_email(email)
        if live:
            sess.refresh()
        return sess

    def with_email(self, email: str) -> Session:
        """Fixa o e-mail da conta (fonte do perfil de aparelho)."""
        self.email = email
        return self

    def _who(self) -> str:
        return self.email or self.data.get("email") or "esta conta"

    def _bearer(self) -> str:
        return str(self.data.get("idToken") or self.data.get("id_token") or "")

    @property
    def id_token(self) -> str:
        """idToken vivo. Troca no Firebase se esta instância ainda não
        renovou ou se falta menos de 10 min para o exp do JWT."""
        remaining = _token_expiry(self.data) - time.time()
        if (not getattr(self, "_refreshing", False)
                and (not self._live or remaining <= _REFRESH_SKEW_S)):
            self.refresh()
        token = self._bearer()
        if not token:
            raise AuthError(
                f"token vazio para {self._who()} — refaça o login "
                "(python3 scripts/minute_cli.py login <email> <senha>)."
            )
        return token

    def _reload_from_disk(self) -> None:
        """Outra thread pode ter rotacionado o refreshToken no mesmo arquivo."""
        if not self.token_file or not self.token_file.exists():
            return
        try:
            disk = json.loads(self.token_file.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(disk, dict):
            return
        if disk.get("refreshToken") or disk.get("refresh_token"):
            if "idToken" not in disk and "id_token" in disk:
                disk["idToken"] = disk["id_token"]
            self.data = disk
            if not self.email:
                self.email = disk.get("email")

    def _persist(self) -> None:
        if self.token_file:
            save_json(self.token_file, self.data)

    # -- chamadas genéricas --------------------------------------------------
    def request(self, method: str, path: str, body: Any = None) -> tuple[int, str]:
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self.id_token}",
            "Accept": "*/*",
            "Accept-Language": config.ACCEPT_LANGUAGE,
        }
        # Identidade de APARELHO da conta (app 1.22.0 envia X-Device-Id em
        # toda chamada; o UA carrega o iOS daquele aparelho — anti-colusão).
        if self.email:
            headers.update(device_profile.get_profile(self.email).headers())
        else:
            headers["X-App-Version"] = config.APP_VERSION
            headers["User-Agent"] = config.USER_AGENT
        status, text = _request(config.BASE_URL + path, method, headers=headers, body=body)
        if status != 401 or getattr(self, "_refreshing", False):
            return status, text
        # Rede de segurança: o Minute recusou o Bearer. Troca no Firebase;
        # se o token novo também cair 401, re-login com a senha salva.
        self._refreshing = True
        try:
            try:
                self.refresh()
            except AuthError:
                return status, text
            headers["Authorization"] = f"Bearer {self._bearer()}"
            status2, text2 = _request(
                config.BASE_URL + path, method, headers=headers, body=body)
            if status2 != 401:
                return status2, text2
            try:
                self._relogin()
            except AuthError:
                return status2, text2
            headers["Authorization"] = f"Bearer {self._bearer()}"
            return _request(config.BASE_URL + path, method, headers=headers, body=body)
        finally:
            self._refreshing = False

    def request_detailed(
        self, method: str, path: str, body: Any = None,
    ) -> HttpResponse:
        """Como :meth:`request`, incluindo headers e o mesmo refresh de token."""
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self.id_token}",
            "Accept": "*/*",
            "Accept-Language": config.ACCEPT_LANGUAGE,
        }
        if self.email:
            headers.update(device_profile.get_profile(self.email).headers())
        else:
            headers["X-App-Version"] = config.APP_VERSION
            headers["User-Agent"] = config.USER_AGENT

        response = _request_detailed(
            config.BASE_URL + path, method, headers=headers, body=body)
        if response.status != 401 or getattr(self, "_refreshing", False):
            return response
        self._refreshing = True
        try:
            try:
                self.refresh()
            except AuthError:
                return response
            headers["Authorization"] = f"Bearer {self._bearer()}"
            refreshed = _request_detailed(
                config.BASE_URL + path, method, headers=headers, body=body)
            if refreshed.status != 401:
                return refreshed
            try:
                self._relogin()
            except AuthError:
                return refreshed
            headers["Authorization"] = f"Bearer {self._bearer()}"
            return _request_detailed(
                config.BASE_URL + path, method, headers=headers, body=body)
        finally:
            self._refreshing = False

    def get(self, path: str) -> tuple[int, str]:
        return self.request("GET", path)

    def post(self, path: str, body: Any = None) -> tuple[int, str]:
        return self.request("POST", path, body)

    def json(self, method: str, path: str, body: Any = None) -> Any:
        """Como request(), mas devolve o corpo já parseado (ou {} se não-JSON)."""
        _, text = self.request(method, path, body)
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return {}

    # -- atalhos de domínio --------------------------------------------------
    def me(self) -> Any:
        return self.json("GET", "/api/v1/users/me")

    def organizations(self) -> Any:
        return self.json("GET", "/api/v1/organizations")

    def categories(self) -> Any:
        """Categorias globais do Minute (base para o enquadramento)."""
        return self.json("GET", "/api/v1/categories")

    def org_tasks(self, org_key: str) -> Any:
        return self.json("GET", f"/api/v1/orgs/{org_key}/tasks")

    def org_quota(self, org_key: str) -> Any:
        return self.json("GET", f"/api/v1/orgs/{org_key}/quota")

    def join_org(self, code: str) -> tuple[int, str]:
        return self.post("/api/v1/organizations/join", {"code": code})

    def create_org(self, name: str, include_default_tasks: bool = True) -> tuple[int, str]:
        return self.post(
            "/api/v1/organizations",
            {"name": name, "include_default_tasks": include_default_tasks},
        )

    # -- autenticação / robustez --------------------------------------------
    def _relogin(self) -> Session:
        """Login com senha salva. Usado quando o refreshToken não convence o Minute."""
        email = self._who()
        lock = _lock_for(self.token_file) if self.token_file else self._lock
        with lock:
            password = _lookup_password(email)
            if not password:
                raise AuthError(
                    f"{email}: sem senha salva para re-login "
                    "(secrets/crowtado_passwords.json)."
                )
            try:
                self.data = login(email, password)
            except RuntimeError as login_exc:
                raise AuthError(
                    f"{email}: login com senha salva falhou: {login_exc}"
                ) from login_exc
            self._persist()
            self._live = True
        return self

    def refresh(self) -> Session:
        """Troca o idToken no Firebase e persiste. Sempre dinâmico.

        1. Recarrega o arquivo (refreshToken pode ter rotacionado).
        2. POST securetoken.googleapis.com com o refreshToken.
        3. Se o refresh falhar, tenta login com a senha salva da conta.
        """
        email = self._who()
        lock = _lock_for(self.token_file) if self.token_file else self._lock
        with lock:
            self._reload_from_disk()
            try:
                self.data = _refresh(self.data)
            except AuthError as exc:
                try:
                    self._relogin()
                except AuthError as login_exc:
                    raise AuthError(f"{email}: {exc}") from login_exc
            self._persist()
            self._live = True
        return self

    def ensure_auth(self) -> dict[str, Any]:
        """Garante que a sessão está válida chamando /users/me.

        Sempre troca o idToken no Firebase antes da 1ª chamada desta
        instância — o JWT estático do disco não é enviado. Um HTTP 200 não
        basta: o HUB mantém o perfil consultável mesmo quando a conta foi
        desativada, mas bloqueia sessão/upload com 403. Detectar ``disabled``
        aqui evita iniciar uma campanha que falharia em cada clipe.

        Levanta AuthError com instrução clara se a conta não autenticar (ex.:
        refresh token expirado) ou estiver desativada no HUB.
        """
        if not self._live:
            self.refresh()
        status, body = self.request("GET", "/api/v1/users/me")
        email = self._who()
        if status == 200:
            try:
                profile = json.loads(body)
            except (json.JSONDecodeError, ValueError) as exc:
                raise AuthError(
                    f"resposta inválida ao validar {email}: {body[:200]}"
                ) from exc
            if profile.get("disabled") is True:
                who = profile.get("email") or email
                raise AuthError(
                    f"conta desativada no HUB: {who}. "
                    "A plataforma precisa reativá-la antes de novos envios."
                )
            return profile
        raise AuthError(
            f"{email}: sessão inválida ({status}) — o token pode ter expirado. "
            f"Refaça o login: python3 scripts/minute_cli.py login {email} <senha>. "
            f"Detalhe: {body[:200]}"
        )

    # -- orgs / tasks (respostas normalizadas) -------------------------------
    def my_orgs(self) -> list[dict[str, Any]]:
        """Organizações do usuário (a partir de /users/me)."""
        return (self.me() or {}).get("organizations") or []

    def all_tasks(self, org_key: str) -> list[dict[str, Any]]:
        """Tasks de uma org, sempre como lista (normaliza list/dict)."""
        _, body = self.get(f"/api/v1/orgs/{org_key}/tasks")
        return [t for t in _as_list(body) if isinstance(t, dict)]

    # -- telemetria (comportamento de app aberto) ----------------------------
    def app_opened(self, auth_method: str = "SESSION_RESUMED",
                   opened_at: str | None = None) -> tuple[int, str]:
        """POST /api/v1/app/opened — telemetria de abertura do app.

        O app nativo publica esse evento ao abrir (captura mitm: app_version +
        device_model + os_version). Sem ele a conta "só existe na hora de
        subir vídeo" — uma das assinaturas de colusão. `auth_method`:
        OAUTH_NEW (login novo) ou SESSION_RESUMED (abriu com sessão viva).
        Sucesso = 202 Accepted (evento na fila para o parceiro).
        """
        if not self.email:
            return -1, "ERRO: sessão sem e-mail (perfil de aparelho ausente)"
        profile = device_profile.get_profile(self.email)
        return self.post("/api/v1/app/opened",
                         profile.opened_payload(auth_method, opened_at))

    def fetch_recording_config(self) -> tuple[int, str]:
        """GET /devices/recording-config — o app consulta ao abrir/gravar.

        Resposta real (captura 06/08): {configVersion:2, backlogCapMs:14400000,
        minDurationMs:60000, maxDurationMs:1800000}. Best-effort: o resultado
        não afeta o upload.
        """
        return self.get("/api/v1/devices/recording-config")
