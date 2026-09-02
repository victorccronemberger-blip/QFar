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
import re
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
_WARMED_IDENTITIES: set[str] = set()
_WARMED_IDENTITIES_GUARD = threading.Lock()

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
    impersonate Chrome/Android se instalado, senão urllib stdlib. Nunca levanta
    exceção de rede: em erro devolve (-1, "ERRO: ...").
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


def _is_geo_route(path: str) -> bool:
    """Rotas em que o app manda `X-Device-Location` (quota/elegibilidade geo).

    O app Android envia o header de localização SÓ nessas (DETALHAMENTO §2.1),
    não em toda chamada autenticada.
    """
    lower = path.casefold()
    return ("quota" in lower or "eligibility" in lower)


# --- Version gate (kill-switch por 403, réplica do maybeLatchVersionGate) -----

def _version_gate_file() -> Path:
    config.DATA_DIR.mkdir(exist_ok=True, parents=True)
    return config.DATA_DIR / "version-gate.json"


def _maybe_latch_version_gate(text: str, *, clear: bool = False) -> None:
    """Persiste `minVersion` localmente após um 403 de version gate.

    Qualquer 403 pode disparar a trava no app (`maybeLatchVersionGate` →
    MMKV `version-gate`). Conservador: só trava se o corpo parecer version
    gate. `clear=True` remove a trava (revert explicit).
    """
    if clear:
        _version_gate_file().unlink(missing_ok=True)
        return
    try:
        body = json.loads(text) if text and text.strip() else {}
    except (json.JSONDecodeError, ValueError):
        body = {}
    if not isinstance(body, dict):
        return
    flat = json.dumps(body, ensure_ascii=False).casefold()
    if not any(token in flat for token in (
            "app_version", "minversion", "app_version_too_old", "update",
            "minimum version", "minimum_app_version")):
        return
    min_version = (
        body.get("minVersion")
        or body.get("minimum_app_version")
        or body.get("app_version_min")
        or body.get("required_version")
        or (body.get("detail") or {}).get("minVersion")
        or (body.get("detail") or {}).get("min_version")
        or config.APP_VERSION
    )
    try:
        _version_gate_file().write_text(json.dumps({
            "minVersion": str(min_version),
            "appVersion": config.APP_VERSION,
            "latchedAt": int(time.time()),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _semver_tuple(value: str) -> tuple[int, int, int]:
    """Compara versões '1.22.0' de forma tolerante a sufixos."""
    import re as _re
    parts = [int(p) for p in _re.sub(r"[^0-9.]", "", str(value)).split(".") if p]
    parts = parts[:3] or [0, 0, 0]
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2])


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

    Usa os headers de identidade do perfil da conta (X-Device-Id Android
    `android.ssaid:...` + UA Android) e envia o `device_id` no corpo — o
    schema WebRegisterRequest da spec tem o campo dedicado e a conta nasce
    associada a UM aparelho.

    Devolve o dict do token. Levanta RuntimeError se o registro falhar.
    """
    profile = device_profile.get_profile(email)
    headers = profile.headers(include_location=False)
    status, body = _request(
        config.BASE_URL + "/api/v1/auth/web-register",
        "POST",
        headers=headers,
        body={
            "email": email,
            "password": password,
            "code": code,
            "device_id": profile.device_id,
        },
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
                f"nenhum acesso salvo para {email}; adicione ou reautentique "
                "a conta pela aba Contas do QMoney"
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
                "pela aba Contas do QMoney."
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
        # Identidade de APARELHO da conta (app 1.22.0 envia X-Device-Id
        # `android.ssaid:...` em toda chamada; o UA carrega o Android daquele
        # aparelho — anti-colusão). A localização (X-Device-Location) só vai
        # nas rotas de quota/geo, como no app.
        if self.email:
            headers.update(device_profile.get_profile(self.email).headers(
                include_location=_is_geo_route(path)))
        else:
            headers["X-App-Version"] = config.APP_VERSION
            headers["User-Agent"] = config.USER_AGENT
        status, text = _request(config.BASE_URL + path, method, headers=headers, body=body)
        if status == 403:
            # Kill-switch de versão mínima: o app trava qualquer 403 que pareça
            # version gate (maybeLatchVersionGate); aqui persistem localmente.
            _maybe_latch_version_gate(text)
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
            headers.update(device_profile.get_profile(self.email).headers(
                include_location=_is_geo_route(path)))
        else:
            headers["X-App-Version"] = config.APP_VERSION
            headers["User-Agent"] = config.USER_AGENT

        response = _request_detailed(
            config.BASE_URL + path, method, headers=headers, body=body)
        if response.status == 403:
            _maybe_latch_version_gate(response.text)
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
        lang = config.ACCEPT_LANGUAGE.split(",", 1)[0].strip()
        query = f"?lang={lang}" if lang else ""
        return self.json("GET", f"/api/v1/orgs/{org_key}/tasks{query}")

    def org_quota(self, org_key: str) -> Any:
        return self.json("GET", f"/api/v1/orgs/{org_key}/quota")

    def join_org(self, code: str) -> tuple[int, str]:
        return self.post("/api/v1/organizations/join", {"code": code})

    def create_org(self, name: str, include_default_tasks: bool = True) -> tuple[int, str]:
        return self.post(
            "/api/v1/organizations",
            {"name": name, "include_default_tasks": include_default_tasks},
        )

    # -- gates do app (qualidade/versão/dispositivo) -------------------------
    def version_gate(self) -> dict[str, Any] | None:
        """Trava de versão mínima latched localmente (replica MMKV `version-gate`)."""
        path = _version_gate_file()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError, OSError):
            return None
        return data if isinstance(data, dict) else None

    def clear_version_gate(self) -> None:
        """Reverte a trava de versão (ex.: após atualizar o QMoney)."""
        _maybe_latch_version_gate("", clear=True)

    def quality_state(self, org_key: str) -> dict[str, Any]:
        """Estado de qualidade do próprio usuário autenticado.

        A rota pública para o usuário atual é ``quality-screen``. A variante
        ``users/{user_key}/quality-scores`` existe, mas a própria OpenAPI a
        restringe a administradores da organização e data overlords.
        """
        data = self.json(
            "GET", f"/api/v1/organizations/{org_key}/quality-screen")
        return data if isinstance(data, dict) else {}

    def org_state(self, org_key: str) -> dict[str, Any]:
        """Estado operacional do usuário numa org (disabled + userState).

        Fontes (OpenAPI de referência):
          - /users/me → {disabled (conta), organizations[]}
          - /organizations/{org}/quality-screen → userState

        `blocked=True` só para a ORG ALVO desativada ou userState
        on_hold/inactive — uma org desativada entre várias NÃO bloqueia.
        """
        profile = self.me() or {}
        org = next(
            (o for o in (profile.get("organizations") or [])
             if isinstance(o, dict) and o.get("resourceKey") == org_key),
            None)
        disabled = profile.get("disabled") is True or (
            isinstance(org, dict) and org.get("disabled") is True)
        summary: dict[str, Any] = {}
        try:
            summary = self.quality_state(org_key)
        except Exception:  # noqa: BLE001 — gate é best-effort
            pass
        if not isinstance(summary, dict):
            summary = {}
        user_state = str(summary.get("userState") or "unknown").lower()
        return {
            "org_key": org_key,
            "disabled": disabled,
            "userState": user_state,
            "overall": summary.get("overallBandCode", summary.get("overall")),
            "blocked": disabled or user_state in ("on_hold", "inactive"),
            "cameraSources": (org or {}).get("cameraSources") or [],
        }

    def camera_policy(self) -> dict[str, Any]:
        """GET /devices/native-camera-policy — allowlist remota de dispositivos."""
        data = self.json("GET", "/api/v1/devices/native-camera-policy")
        return data if isinstance(data, dict) else {}

    def camera_model_allowed(self, model: str) -> bool | None:
        """Decide se um Build.MODEL passa na allowlist nativa (None = sem policy)."""
        policy = self.camera_policy()
        if not policy:
            return None

        def _norm(value: str) -> str:
            result = str(value or "")
            norm = (policy.get("normalization") or {})
            if norm.get("trimWhitespace"):
                result = result.strip()
            if norm.get("lowercase"):
                result = result.lower()
            if norm.get("collapseInternalWhitespace"):
                result = re.sub(r"\s+", " ", result)
            return result

        normalized = _norm(model)
        denied = {_norm(m) for m in (policy.get("androidDeniedModels") or [])}
        if normalized in denied:
            return False
        allowed = {_norm(m) for m in (policy.get("androidAllowModels") or [])}
        if normalized in allowed:
            return True
        for pattern in (policy.get("androidAllowModelPatterns") or []):
            try:
                if re.search(_norm(pattern), normalized) \
                        or re.fullmatch(str(pattern), model or ""):
                    return True
            except re.error:
                continue
        return False

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

    def ensure_auth(self, *, org_key: str | None = None) -> dict[str, Any]:
        """Garante que a sessão está válida chamando /users/me.

        Sempre troca o idToken no Firebase antes da 1ª chamada desta
        instância — o JWT estático do disco não é enviado. Um HTTP 200 não
        basta: o HUB mantém o perfil consultável mesmo quando a conta foi
        desativada, mas bloqueia sessão/upload com 403. Detectar ``disabled``
        aqui evita iniciar uma campanha que falharia em cada clipe.

        Com `org_key`, replica as travas do app para aquela org:
          - `quality-screen` (userState on_hold/inactive) → conta parada;
          - version gate latch local (403) → versão travada.

        Levanta AuthError com instrução clara se a conta não autenticar (ex.:
        refresh token expirado), estiver desativada, em hold, ou com versão
        bloqueada.
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
            # Gate de versão (kill-switch via 403) — independente de org.
            gate = self.version_gate()
            if gate and gate.get("minVersion"):
                if _semver_tuple(config.APP_VERSION) < \
                        _semver_tuple(str(gate["minVersion"])):
                    raise AuthError(
                        f"{email}: versão {config.APP_VERSION} bloqueada pelo "
                        f"backend (mínimo {gate['minVersion']}). Atualize o "
                        "QMoney antes de novos envios."
                    )
            # Bloqueio POR ORG ALVO (disabled da conta/org + userState). Uma org
            # desativada entre várias NÃO bloqueia a conta inteira — só a org
            # para a qual o envio realmente vai.
            if org_key:
                state = self.org_state(org_key)
                if state["blocked"]:
                    what = ("org desativada" if state["disabled"]
                            else f"conta {state['userState']}"
                            if state["userState"] in ("on_hold", "inactive")
                            else "indisponível")
                    raise AuthError(
                        f"{email}: {what} para {org_key} — a plataforma não "
                        "aceita envios agora (o app Minute pararia aqui)."
                    )
            elif profile.get("disabled") is True:
                who = profile.get("email") or email
                raise AuthError(
                    f"conta desativada no HUB: {who}. "
                    "A plataforma precisa reativá-la antes de novos envios."
                )
            self.warmup()
            return profile
        raise AuthError(
            f"{email}: sessão inválida ({status}) — o token pode ter expirado. "
            "Refaça o login pela aba Contas do QMoney. "
            f"Detalhe: {body[:200]}"
        )

    # -- orgs / tasks (respostas normalizadas) -------------------------------
    def my_orgs(self) -> list[dict[str, Any]]:
        """Organizações do usuário (a partir de /users/me)."""
        return (self.me() or {}).get("organizations") or []

    def all_tasks(self, org_key: str) -> list[dict[str, Any]]:
        """Tasks de uma org, sempre como lista (normaliza list/dict)."""
        lang = config.ACCEPT_LANGUAGE.split(",", 1)[0].strip()
        query = f"?lang={lang}" if lang else ""
        _, body = self.get(f"/api/v1/orgs/{org_key}/tasks{query}")
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

    def warmup(self) -> None:
        """Replica a abertura do app: telemetria + limites de gravação.

        Best-effort: falha de rede não derruba o upload. Roda uma vez por
        conta durante esta execução do motor, mesmo que a interface crie
        várias instâncias de Session ao recarregar telas.
        """
        if getattr(self, "_warmed", False):
            return
        identity = str(self.email or self.data.get("email") or "").casefold()
        if identity:
            with _WARMED_IDENTITIES_GUARD:
                if identity in _WARMED_IDENTITIES:
                    self._warmed = True
                    return
                _WARMED_IDENTITIES.add(identity)
        self._warmed = True
        # VPN: o app nega toda chamada autenticada com VPN ativa (assertNoVpn).
        # QMoney avisa (padrão) ou trava se MINUTE_VPN_ENFORCE=1.
        try:
            from . import vpn as _vpn
            if _vpn.vpn_active():
                self.vpn_active = True
                print("[vpn] " + _vpn.vpn_message(), flush=True)
                if _vpn.ENFORCE:
                    raise AuthError(
                        f"{self._who()}: " + _vpn.vpn_message()
                    )
        except AuthError:
            raise
        except Exception:  # noqa: BLE001 — checagem best-effort
            pass
        # Transporte: OKHttp/Android exige curl_cffi; o fallback urllib tem
        # fingerprint de Python. Avisa (e trava se MINUTE_REQUIRE_CURL=1).
        try:
            if transport.kind() == "urllib":
                self.transport_weak = True
                print(
                    "[transport] curl_cffi indisponível → fallback urllib "
                    "(fingerprint de Python, não OkHttp). Instale curl_cffi "
                    "para o perfil Chrome/Android.", flush=True)
                if config.REQUIRE_CURL:
                    raise AuthError(
                        f"{self._who()}: curl_cffi ausente e "
                        "MINUTE_REQUIRE_CURL=1 — abortando (fingerprint "
                        "inseguro)."
                    )
        except AuthError:
            raise
        except Exception:  # noqa: BLE001
            pass
        try:
            self.app_opened(
                "SESSION_RESUMED",
                opened_at=device_profile.format_recorded_at(time.time()))
        except Exception:  # noqa: BLE001
            pass
        try:
            status, body = self.fetch_recording_config()
            if status == 200:
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    self.recording_config = parsed
                    # Aplica os limites remotos (min/max/backlog) igual ao app.
                    config.apply_recording_config(parsed)
        except Exception:  # noqa: BLE001
            pass
        # Allowlist de dispositivos: best-effort, para diagnóstico.
        try:
            model = device_profile.get_profile(self.email).device_model \
                if self.email else ""
            if model:
                self.device_camera_allowed = self.camera_model_allowed(model)
                if self.device_camera_allowed is False:
                    print(
                        f"[policy] Build.MODEL {model} NÃO está na allowlist "
                        "nativa (native-camera-policy).", flush=True)
        except Exception:  # noqa: BLE001
            pass
