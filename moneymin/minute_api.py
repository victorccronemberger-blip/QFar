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
from urllib.parse import urlencode, urlsplit, parse_qs

from . import config, device_profile, transport
from .atomic_io import save_json
from .service_policy import RecordingPolicy

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

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.account_issue_code = code


def _auth_failure(status: int, body: str, stage: str, *, firebase: bool = False) -> AuthError:
    """Preserva a causa sem confundir indisponibilidade com senha ou bloqueio."""
    raw = (body or "").casefold()
    code = "unknown"
    if status == 429:
        code = "rate_limit"
    elif status >= 500:
        code = "service"
    elif status == 408 or (status == -1 and any(s in raw for s in ("timeout", "timed out"))):
        code = "timeout"
    elif status == -1:
        code = "tls" if any(s in raw for s in ("certificate", "ssl", "tls")) else "network"
    elif status == 401:
        code = "authentication"
    elif status == 403:
        code = _classify_minute_403(body) or "forbidden"
    if firebase and status in (400, 401, 403):
        try:
            payload = json.loads(body)
            error = payload.get("error") if isinstance(payload, dict) else None
            message = error.get("message") if isinstance(error, dict) else error
        except (ValueError, TypeError):
            message = None
        if message == "USER_DISABLED":
            code = "restricted"
        elif message in ("INVALID_PASSWORD", "INVALID_LOGIN_CREDENTIALS", "INVALID_REFRESH_TOKEN",
                         "TOKEN_EXPIRED", "USER_NOT_FOUND", "EMAIL_NOT_FOUND", "INVALID_GRANT"):
            code = "authentication"
        elif message == "TOO_MANY_ATTEMPTS_TRY_LATER":
            code = "rate_limit"
    return AuthError(f"{stage}: resposta HTTP {status}; verificação não concluída.", code=code)


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
# Hermes: detail.error === 'app_version_too_old' e detail.min_version parseável.

_APP_VERSION_TOO_OLD = "app_version_too_old"
_BLOCKED_DETAIL_ERRORS = {
    _APP_VERSION_TOO_OLD: "version",
    "device": "device",
    "uber-device": "uber_device",
}


def _version_gate_file() -> Path:
    config.DATA_DIR.mkdir(exist_ok=True, parents=True)
    return config.DATA_DIR / "version-gate.json"


def _parse_blocked_detail(text: str) -> dict[str, Any] | None:
    """Extrai `detail` de um corpo 403 no formato do app (`blockedDetailSchema`)."""
    try:
        body = json.loads(text) if text and str(text).strip() else {}
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(body, dict):
        return None
    detail = body.get("detail")
    return detail if isinstance(detail, dict) else None


def _classify_minute_403(text: str) -> str | None:
    """Mapeia `detail.error` do Minute para código de diagnóstico."""
    detail = _parse_blocked_detail(text)
    if not detail:
        return None
    error = detail.get("error")
    if not isinstance(error, str):
        return None
    return _BLOCKED_DETAIL_ERRORS.get(error)


def _parse_semver_or_none(value: Any) -> tuple[int, int, int] | None:
    """Aceita somente os três componentes numéricos do contrato Hermes."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        return None
    try:
        major, minor, patch = map(int, value.split("."))
    except ValueError:
        return None
    return major, minor, patch


def _maybe_latch_version_gate(text: str, *, clear: bool = False) -> None:
    """Persiste `min_version` só no contrato do APK (`maybeLatchVersionGate`).

    Trava apenas quando `detail.error === app_version_too_old` e
    `detail.min_version` é um semver parseável. `clear=True` remove a trava.
    """
    if clear:
        _version_gate_file().unlink(missing_ok=True)
        return
    detail = _parse_blocked_detail(text)
    if not detail or detail.get("error") != _APP_VERSION_TOO_OLD:
        return
    min_version = detail.get("min_version")
    if _parse_semver_or_none(min_version) is None:
        return
    try:
        _version_gate_file().write_text(json.dumps({
            "minVersion": str(min_version).strip(),
            "appVersion": config.APP_VERSION,
            "latchedAt": int(time.time()),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _semver_tuple(value: str) -> tuple[int, int, int]:
    """Compara versões '1.22.0' de forma tolerante a sufixos."""
    return _parse_semver_or_none(value) or (0, 0, 0)


def _version_gate_blocks() -> bool:
    """True se a trava local exige versão maior que a do cliente."""
    path = _version_gate_file()
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError, OSError):
        return False
    if not isinstance(data, dict):
        return False
    minimum = _parse_semver_or_none(data.get("minVersion"))
    current = _parse_semver_or_none(config.APP_VERSION)
    return minimum is not None and current is not None and current < minimum


def _normalize_camera_model(model: Any) -> str:
    """`normalizeModel` do APK: trim + lower + colapso de whitespace."""
    if model is None:
        return ""
    return re.sub(r"\s+", " ", str(model).strip().lower())


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
def _validate_token_response(data: Any, token_key: str, refresh_key: str,
                             expiry_key: str) -> int:
    """Valida a resposta inteira antes de persistir ou substituir credenciais."""
    if not isinstance(data, dict) or any(
        not isinstance(data.get(key), str) or not data[key].strip()
        for key in (token_key, refresh_key)
    ):
        raise AuthError("Resposta de autenticação inválida.", code="invalid_response")
    value = data.get(expiry_key)
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise AuthError("Validade da sessão inválida.", code="invalid_response")
    try:
        seconds = int(value)
    except ValueError as exc:
        raise AuthError("Validade da sessão inválida.", code="invalid_response") from exc
    if seconds <= 0:
        raise AuthError("Validade da sessão inválida.", code="invalid_response")
    return seconds


def login(email: str, password: str) -> dict[str, Any]:
    """Autentica no Firebase e grava `secrets/token_<email>.json`.

    Devolve o dict do token. Levanta RuntimeError se o login falhar.
    """
    from .account_bans import require_not_banned
    require_not_banned(email)
    status, body = _request(
        _SIGNIN_URL,
        "POST",
        body={"email": email, "password": password, "returnSecureToken": True},
    )
    if status != 200:
        raise _auth_failure(status, body, "Login", firebase=True)
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError) as exc:
        raise AuthError("Login devolveu resposta inválida.", code="invalid_response") from exc

    expires_in = _validate_token_response(data, "idToken", "refreshToken", "expiresIn")
    data["expires_at"] = _expiry_from_token(
        data["idToken"], expires_in
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
    from .account_bans import require_not_banned
    require_not_banned(email)
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
            if str(rec.get("email") or "").strip().casefold() == email.strip().casefold() and rec.get("senha"):
                found = str(rec["senha"])
    except OSError:
        pass
    pw_path = config.SECRETS_DIR / "crowtado_passwords.json"
    try:
        creds = json.loads(pw_path.read_text(encoding="utf-8-sig"))
        if isinstance(creds, dict):
            for key, value in creds.items():
                if str(key).strip().casefold() == email.strip().casefold() and isinstance(value, str) and value:
                    found = value
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
        raise AuthError("sem refreshToken no arquivo — reconecte o acesso.", code="authentication")
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
        raise _auth_failure(status, body, "Renovação da sessão", firebase=True)
    try:
        resp = json.loads(body) if (body or "").strip() else None
    except json.JSONDecodeError as exc:
        raise AuthError(
            f"refresh devolveu resposta vazia/não-JSON ({status}).", code="invalid_response",
        ) from exc
    expires_in = _validate_token_response(resp, "id_token", "refresh_token", "expires_in")
    expires_at = _expiry_from_token(resp["id_token"], expires_in)
    token_data.update(idToken=resp["id_token"], refreshToken=resp["refresh_token"],
                      expiresIn=str(expires_in), expires_at=expires_at)
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
        self.recording_policy: RecordingPolicy | None = None
        self.initialization_errors: dict[str, str] = {}
        self._recording_checked_at: float | None = None
        self._recording_retry_at = 0.0
        self._quota_cache: dict[str, tuple[float, dict[str, Any]]] = {}

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
        with self._lock:
            if str(self.email or "").casefold() != email.casefold():
                self.recording_policy = None
                self.recording_config = {}
                self.device_camera_allowed = None
                self.initialization_errors = {}
                self._recording_checked_at = None
                self._recording_retry_at = 0.0
                self._initialization_failures = 0
                self._quota_cache = {}
                self._app_opened_published = False
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
        self._check_write_policy(method, path, body)
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
                raise
            headers["Authorization"] = f"Bearer {self._bearer()}"
            status2, text2 = _request(
                config.BASE_URL + path, method, headers=headers, body=body)
            if status2 == 403:
                _maybe_latch_version_gate(text2)
            if status2 != 401:
                return status2, text2
            try:
                self._relogin()
            except AuthError:
                raise
            headers["Authorization"] = f"Bearer {self._bearer()}"
            status3, text3 = _request(config.BASE_URL + path, method, headers=headers, body=body)
            if status3 == 403:
                _maybe_latch_version_gate(text3)
            return status3, text3
        finally:
            self._refreshing = False

    def request_detailed(
        self, method: str, path: str, body: Any = None,
    ) -> HttpResponse:
        """Como :meth:`request`, incluindo headers e o mesmo refresh de token."""
        self._check_write_policy(method, path, body)
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
                raise
            headers["Authorization"] = f"Bearer {self._bearer()}"
            refreshed = _request_detailed(
                config.BASE_URL + path, method, headers=headers, body=body)
            if refreshed.status == 403:
                _maybe_latch_version_gate(refreshed.text)
            if refreshed.status != 401:
                return refreshed
            try:
                self._relogin()
            except AuthError:
                raise
            headers["Authorization"] = f"Bearer {self._bearer()}"
            relogged = _request_detailed(
                config.BASE_URL + path, method, headers=headers, body=body)
            if relogged.status == 403:
                _maybe_latch_version_gate(relogged.text)
            return relogged
        finally:
            self._refreshing = False

    def get(self, path: str) -> tuple[int, str]:
        return self.request("GET", path)

    def post(self, path: str, body: Any = None) -> tuple[int, str]:
        return self.request("POST", path, body)

    def json(self, method: str, path: str, body: Any = None) -> Any:
        """Como request(), mas devolve o corpo já parseado (ou {} se não-JSON)."""
        status, text = self.request(method, path, body)
        if not 200 <= status < 300:
            return {}
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
        """Quota/geo da org (`getRecordingGeo` do app)."""
        return self.recording_geo(org_key)

    def recording_geo(self, org_key: str) -> dict[str, Any]:
        """GET /orgs/{org}/quota — autorização de gravação/geo com cache curto.

        Contrato OpenAPI `QuotaStatusResponse`. Resposta inválida ou HTTP de
        erro devolve `{}` (estado desconhecido — não autoriza upload).
        """
        cached = self._quota_cache.get(org_key)
        now = time.monotonic()
        if cached is not None and now - cached[0] < 60:
            return dict(cached[1])
        status, text = self.get(f"/api/v1/orgs/{org_key}/quota")
        if status != 200:
            return {}
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return {}
        if not isinstance(data, dict):
            return {}
        auth = data.get("recordingAuthorization")
        can = data.get("canUpload")
        reason = data.get("blockedReason")
        if auth not in ("APPROVED", "REQUIRES_LOCATION", "BLOCKED"):
            return {}
        if type(can) is not bool or "blockedReason" not in data:
            return {}
        if reason is not None and reason not in (
                "manual", "quota_exceeded", "geo_restricted", "geo_unknown"):
            return {}
        self._quota_cache[org_key] = (now, data)
        return dict(data)

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
        status, text = self.get(f"/api/v1/organizations/{org_key}/quality-screen")
        if status != 200:
            return {}
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return {}
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
        if not isinstance(profile, dict) or not isinstance(profile.get("organizations"), list):
            raise AuthError("Não foi possível verificar o perfil da organização.", code="invalid_response")
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
        """GET /devices/native-camera-policy — contrato OpenAPI NativeCameraPolicyResponse."""
        status, text = self.get("/api/v1/devices/native-camera-policy")
        if status != 200:
            return {}
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return {}
        if (not isinstance(data, dict) or type(data.get("policyVersion")) is not int
                or data["policyVersion"] < 1):
            return {}
        for key in ("iosAllowModels", "iosDeniedModels",
                    "androidAllowModels", "androidAllowModelPatterns"):
            if not isinstance(data.get(key), list) or any(not isinstance(item, str) for item in data[key]):
                return {}
        normalization = data.get("normalization")
        if not isinstance(normalization, dict) or any(type(normalization.get(key)) is not bool for key in
                ("trimWhitespace", "lowercase", "collapseInternalWhitespace")):
            return {}
        return data

    def camera_model_allowed(self, model: str) -> bool | None:
        """Réplica de `getNativeCameraDecision` (Android) do app 1.22.0.

        Sempre normaliza o modelo (trim + lower + colapso de espaços), compara
        com `androidAllowModels` crus e aplica `RegExp(pattern).test(model)`
        via `re.search`. Não há deny-list Android no contrato OpenAPI/Hermes.
        """
        policy = self.camera_policy()
        if not policy:
            return None
        normalized = _normalize_camera_model(model)
        allowed = policy.get("androidAllowModels") or []
        if normalized in allowed:
            return True
        for pattern in (policy.get("androidAllowModelPatterns") or []):
            try:
                if re.search(str(pattern), normalized) is not None:
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
                    "(secrets/crowtado_passwords.json).", code="missing_access",
                )
            try:
                self.data = login(email, password)
            except AuthError:
                raise
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
                if exc.account_issue_code and exc.account_issue_code != "authentication":
                    raise
                try:
                    self._relogin()
                except AuthError as login_exc:
                    raise login_exc from exc
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

        O latch de versão é aplicado às mutações, preservando esta leitura
        para diagnóstico.

        Levanta AuthError com instrução clara se a conta não autenticar (ex.:
        refresh token expirado), estiver desativada ou em hold.
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
                    f"resposta inválida ao validar {email}", code="invalid_response",
                ) from exc
            if not isinstance(profile, dict) or not isinstance(profile.get("organizations"), list):
                raise AuthError("O perfil devolvido pelo serviço está incompleto.", code="invalid_response")
            # A autorização de mutações é verificada no write path.
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
                        "aceita envios agora (o app Minute pararia aqui).", code="restricted",
                    )
            elif profile.get("disabled") is True:
                who = profile.get("email") or email
                raise AuthError(
                    f"conta desativada no HUB: {who}. "
                    "A plataforma precisa reativá-la antes de novos envios.", code="restricted",
                )
            self.warmup()
            return profile
        raise _auth_failure(status, body, "Consulta do perfil")

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

        Método legado, não usado na inicialização do cliente Windows.
        O evento descreve uma abertura do app Android e depende de dados
        verdadeiros de execução; não deve ser usado para simular atividade.
        """
        if not self.email:
            return -1, "ERRO: sessão sem e-mail (perfil de aparelho ausente)"
        profile = device_profile.get_profile(self.email)
        return self.post("/api/v1/app/opened",
                         profile.opened_payload(auth_method, opened_at))

    def fetch_recording_config(self) -> tuple[int, str]:
        """GET /devices/recording-config — o app consulta ao abrir/gravar.

        A resposta é validada por RecordingPolicy e mantida nesta sessão.
        Falhas de consulta impedem o registro de novos uploads até recuperação.
        """
        return self.get("/api/v1/devices/recording-config")

    def _check_local_restrictions(self) -> None:
        from . import vpn
        if vpn.ENFORCE:
            active = vpn.vpn_active()
            if active is None:
                raise AuthError("Não foi possível verificar a política de VPN. Tente novamente.", code="service")
            if active:
                raise AuthError("Operação bloqueada pela política de VPN configurada.", code="policy")
        if config.REQUIRE_CURL and transport.kind() == "urllib":
            raise AuthError("Transporte obrigatório indisponível.", code="service")

    def _check_write_policy(self, method: str, path: str, body: Any) -> None:
        self._check_local_restrictions()
        method_u = method.upper()
        # Latch de versão é app-wide no APK; leituras seguem liberadas p/ diagnóstico.
        if method_u in ("POST", "PUT", "PATCH", "DELETE") and _version_gate_blocks():
            raise AuthError("Versão recusada pelo serviço. Atualize antes de enviar.", code="version")
        route = path.split("?", 1)[0].rstrip("/")
        if method_u != "POST" or route != "/api/v1/uploads":
            return
        self.warmup()
        if self.recording_policy is None or "recording_config" in self.initialization_errors:
            raise AuthError("Não foi possível validar os limites desta sessão. Tente novamente.", code="service")
        if getattr(self, "device_camera_allowed", None) is not True:
            code = "policy" if getattr(self, "device_camera_allowed", None) is False else "service"
            raise AuthError("Política de câmera não autorizada ou não verificada para esta sessão.", code=code)
        org_key = (parse_qs(urlsplit(path).query).get("org_key") or [""])[0]
        if not org_key:
            raise AuthError("Organização ausente no registro de upload.", code="policy")
        state = self.org_state(org_key)
        if state["blocked"]:
            raise AuthError("A organização ou a conta não permite novos envios.", code="restricted")
        if state["userState"] != "active":
            raise AuthError("Não foi possível verificar a situação da conta na organização.", code="service")
        self._check_recording_geo(org_key)
        meta = body.get("meta") if isinstance(body, dict) else None
        sources = state.get("cameraSources")
        if not isinstance(sources, list) or not sources:
            raise AuthError("Origens de gravação permitidas não foram confirmadas.", code="service")
        # meta.source descreve o formato (ego), não a origem da câmera.
        # A origem declarada de cada câmera está em meta.cameras[].source.
        cameras = meta.get("cameras") if isinstance(meta, dict) else None
        if not isinstance(cameras, list) or not cameras:
            raise AuthError("Origem da câmera ausente nos metadados do envio.", code="policy")
        camera_sources = []
        for camera in cameras:
            source = camera.get("source") if isinstance(camera, dict) else None
            if source not in ("builtin", "built-in", "external"):
                raise AuthError("Origem da câmera inválida nos metadados do envio.", code="policy")
            camera_sources.append("built-in" if source == "builtin" else source)
        if any(source not in sources for source in camera_sources):
            raise AuthError("Origem da gravação não permitida pela organização.", code="policy")
        try:
            self.recording_policy.validate_duration(body.get("duration_ms") if isinstance(body, dict) else None)
            self.recording_policy.validate_recording_time(body.get("recorded_at"), body["duration_ms"], time.time())
        except ValueError as exc:
            raise AuthError(str(exc), code="policy") from exc

    def _check_recording_geo(self, org_key: str) -> None:
        """Respeita `recordingAuthorization` / `canUpload` do `/orgs/.../quota`."""
        quota = self.recording_geo(org_key)
        if not quota:
            raise AuthError(
                "Não foi possível verificar a autorização geográfica ou de quota.",
                code="service",
            )
        auth = quota.get("recordingAuthorization")
        can = quota.get("canUpload")
        reason = quota.get("blockedReason")
        if auth == "REQUIRES_LOCATION":
            raise AuthError(
                "O serviço exige localização do dispositivo para novos envios.",
                code="policy",
            )
        if auth == "BLOCKED" or can is False or reason in (
                "geo_restricted", "geo_unknown", "quota_exceeded", "manual"):
            label = reason or auth or "blocked"
            raise AuthError(
                f"Novos envios bloqueados pela autorização de gravação ({label}).",
                code="policy",
            )
        if auth == "APPROVED" and can is True and "blockedReason" in quota and reason is None:
            return
        raise AuthError(
            "Não foi possível confirmar autorização de gravação para esta organização.",
            code="service",
        )

    def warmup(self) -> None:
        """Atualiza políticas de leitura; `app/opened` só com flag explícita.

        Estado e cache pertencem à sessão. Respostas inválidas não são sucesso;
        uma próxima chamada tenta novamente com espera limitada. Políticas
        vencidas que não puderam ser renovadas impedem novos registros.
        Com `MINUTE_PUBLISH_APP_OPENED=1`, publica um `SESSION_RESUMED` após
        sucesso (uma vez por conta nesta sessão; falha tenta no próximo ciclo).
        """
        self._check_local_restrictions()
        with self._lock:
            now = time.monotonic()
            if self._recording_checked_at is not None and now - self._recording_checked_at < 60:
                return
            if now < self._recording_retry_at:
                return
            self.initialization_errors = {}
            try:
                status, body = self.fetch_recording_config()
                if status != 200:
                    raise ValueError("Consulta de configuração indisponível.")
                payload = json.loads(body)
                self.recording_policy = RecordingPolicy.parse(payload)
                self.recording_config = payload
            except Exception:
                self.initialization_errors["recording_config"] = "Não foi possível validar a configuração de gravação."
            # Preserva a consulta existente, mas uma negativa deixa de ser aviso.
            self.device_camera_allowed = None
            try:
                model = device_profile.get_profile(self.email).device_model if self.email else ""
                if model:
                    self.device_camera_allowed = self.camera_model_allowed(model)
                if self.device_camera_allowed is None:
                    raise ValueError("Política não verificada.")
            except Exception:
                self.initialization_errors["camera_policy"] = "Não foi possível verificar a política de câmera."
            if self.initialization_errors:
                self._recording_checked_at = None
                failures = min(getattr(self, "_initialization_failures", 0) + 1, 5)
                self._initialization_failures = failures
                self._recording_retry_at = time.monotonic() + min(60, 5 * 2 ** (failures - 1))
            else:
                self._initialization_failures = 0
                self._recording_retry_at = 0.0
                self._recording_checked_at = time.monotonic()
                if config.PUBLISH_APP_OPENED and not getattr(self, "_app_opened_published", False):
                    try:
                        status, _ = self.app_opened("SESSION_RESUMED")
                        self._app_opened_published = 200 <= status < 300
                    except Exception:
                        pass
