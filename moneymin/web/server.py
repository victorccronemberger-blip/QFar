"""
server.py — Servico local da interface Qt do QMoney (Flask).

Endpoints JSON consumidos exclusivamente pelo aplicativo desktop:

  Contas
    GET    /api/accounts                 lista contas (token_*.json em secrets/)
    POST   /api/accounts                 {email, password} -> login (adicionar)
    POST   /api/accounts/register        {email, password} -> cria conta no Minute
                                         (convite fixo), salva token + senha p/ Saldos
    DELETE /api/accounts/<email>         remove a conta (apaga o token)
    POST   /api/accounts/<email>/check   valida token + resolve org_key (cacheia)
    POST   /api/accounts/check-all       valida todas em paralelo e separa desativadas

  Categorias (tasks do Minute cruzadas com cenários Ego4D elegíveis)
    GET    /api/tasks?email=...          lista categorias elegíveis p/ campanha

  Preferências (data/webui_prefs.json)
    GET    /api/preferences
    PUT    /api/preferences              merge raso do body nas prefs

  Campanha (uma por vez — ver web.runner)
    POST   /api/campaigns                inicia; 400 body inválido; se ocupado,
                                         devolve a campanha existente (idempotente)
                                         body: {accounts, tasks, count, max_dur_s,
                                         delay_mode (off|clip|fixed), delay_s,
                                         paralelismo automático entre contas,
                                         active_hours: [7, 18] | null}
    GET    /api/campaigns/current?since=N  estado + eventos novos (polling)
    POST   /api/campaigns/stop           parada cooperativa

  Acelerador HoloAssist (pré-cache retomável)
    GET    /api/holo-cache               cobertura local + estado do runner
    POST   /api/holo-cache/start         inicia download/normalização em 2º plano
    POST   /api/holo-cache/stop          para depois do clipe atual

  Armazenamento
    POST   /api/storage/cleanup           remove mídia/IMU baixada e derivados

  Histórico
    GET    /api/logs                     lista data/campaign_*.json (resumo)
    GET    /api/logs/<nome>              detalhe do log
    POST   /api/logs/<nome>/status       consulta status das sessões no backend

  Saldos (crowtado — cache em data/balances.json)
    GET    /api/balances                 saldos cacheados + estado do runner
    POST   /api/balances/refresh         {emails?} -> consulta em 2º plano (409 se ocupado)
    POST   /api/balances/withdraw        {email} -> solicita link de saque ao Dots
    PUT    /api/balances/credentials     {email, password} -> salva senha do crowtado
                                         (secrets/crowtado_passwords.json)

  Registro de enviados (data/sent_videos.json — dedup entre campanhas)
    GET    /api/sent                     resumo por cenário (clipes/envios)
    POST   /api/sent/reset               limpa o registro (body: {scenario?})
"""
from __future__ import annotations

import configparser
import json
import math
import os
import platform
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

from .. import (
    campaign, config, crowtado, ego4d, holo_accelerator, holoassist,
    hostinger_mail, readiness, sent_registry,
)
from ..atomic_io import load_json, save_json
from ..campaign import AccountSpec, CampaignConfig, TaskSpec
from ..minute_api import AuthError, Session, login
from ..secure_store import load_secure_settings, save_secure_settings
from .runner import (
    BALANCES_RUNNER, HOLO_CACHE_RUNNER, RUNNER, friendly_campaign_error,
)

PREFS_PATH = config.DATA_DIR / "webui_prefs.json"
BALANCES_PATH = config.DATA_DIR / "balances.json"
CROWTADO_PW_PATH = config.SECRETS_DIR / "crowtado_passwords.json"
MAX_DUR_S = 1800.0  # cap do recording-config do Minute
_PERSISTENCE_LOCK = threading.RLock()
_HEAVY_RUNNER_LOCK = threading.Lock()
_WITHDRAW_LOCK = threading.Lock()
_WITHDRAW_IN_FLIGHT: set[str] = set()
_WITHDRAW_LAST_REQUEST: dict[str, float] = {}
_WITHDRAW_COOLDOWN_S = 60.0


def _tree_size(path: Path) -> tuple[int, int]:
    """Tamanho/arquivos sem seguir links; falhas pontuais não quebram o painel."""
    total = files = 0
    if not path.exists():
        return total, files
    try:
        for root, _, names in os.walk(path, followlinks=False):
            for name in names:
                try:
                    total += (Path(root) / name).stat().st_size
                    files += 1
                except OSError:
                    continue
    except OSError:
        pass
    return total, files


def _storage_snapshot(*, include_path: bool = True) -> dict[str, Any]:
    data_bytes, data_files = _tree_size(config.MEDIA_DATA_DIR)
    ego_bytes, ego_files = _tree_size(config.MEDIA_DATA_DIR / "ego4d")
    holo_bytes, holo_files = _tree_size(config.MEDIA_DATA_DIR / "holoassist")
    try:
        usage = shutil.disk_usage(config.LIBRARY_ROOT)
        free_bytes, total_bytes = usage.free, usage.total
    except OSError:
        free_bytes = total_bytes = 0
    result: dict[str, Any] = {
        "ready": (
            (config.MEDIA_DATA_DIR / "ego4d" / "timed_narrations.jsonl").exists()
            or (config.MEDIA_DATA_DIR / "ego4d" / "clip_narrations.json").exists()
            or (config.MEDIA_DATA_DIR / "holoassist").exists()
        ),
        "data_bytes": data_bytes,
        "data_files": data_files,
        "ego4d_bytes": ego_bytes,
        "ego4d_files": ego_files,
        "holoassist_bytes": holo_bytes,
        "holoassist_files": holo_files,
        "free_bytes": free_bytes,
        "disk_bytes": total_bytes,
    }
    if include_path:
        result.update({
            "root": str(config.LIBRARY_ROOT),
            "data_dir": str(config.MEDIA_DATA_DIR),
        })
    return result


# --- preferências ------------------------------------------------------------

def _load_prefs() -> dict[str, Any]:
    value = load_json(PREFS_PATH, {})
    return value if isinstance(value, dict) else {}


def _save_prefs(prefs: dict[str, Any]) -> None:
    with _PERSISTENCE_LOCK:
        save_json(PREFS_PATH, prefs)


# --- integrações protegidas -------------------------------------------------

def _legacy_aws_credentials() -> dict[str, str]:
    """Lê credenciais já configuradas sem expô-las para a resposta HTTP."""
    access = os.environ.get("AWS_ACCESS_KEY_ID", "").strip()
    secret = os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip()
    if access and secret:
        return {
            "access_key_id": access,
            "secret_access_key": secret,
            "session_token": os.environ.get("AWS_SESSION_TOKEN", "").strip(),
            "region": config.EGO4D_AWS_REGION or "",
        }
    profile = config.EGO4D_AWS_PROFILE or "default"
    candidates = [
        Path(os.environ.get("AWS_SHARED_CREDENTIALS_FILE", "")),
        config.EGO4D_LOCAL_AWS_CREDENTIALS,
        Path.home() / ".aws" / "credentials",
    ]
    parser = configparser.RawConfigParser()
    for path in candidates:
        if not str(path) or not path.is_file():
            continue
        try:
            parser.read(path, encoding="utf-8")
        except (OSError, configparser.Error):
            continue
        if not parser.has_section(profile):
            continue
        access = parser.get(profile, "aws_access_key_id", fallback="").strip()
        secret = parser.get(profile, "aws_secret_access_key", fallback="").strip()
        if access and secret:
            return {
                "access_key_id": access,
                "secret_access_key": secret,
                "session_token": parser.get(
                    profile, "aws_session_token", fallback="").strip(),
                "region": config.EGO4D_AWS_REGION or "",
            }
    return {}


def _migrate_legacy_integrations() -> dict[str, Any]:
    """Copia configurações existentes para o DPAPI, sem apagar os originais."""
    with _PERSISTENCE_LOCK:
        secure = load_secure_settings(config.INTEGRATIONS_PATH)
        changed = False
        if not isinstance(secure.get("hostinger"), dict) and config.HOSTINGER_MAIL_TOKEN:
            secure["hostinger"] = {
                "token": config.HOSTINGER_MAIL_TOKEN,
                "mailbox_id": config.HOSTINGER_MAILBOX_ID,
            }
            changed = True
        if not isinstance(secure.get("ego4d"), dict):
            legacy = _legacy_aws_credentials()
            if legacy:
                secure["ego4d"] = legacy
                changed = True
        if changed:
            secure["schema"] = 1
            save_secure_settings(config.INTEGRATIONS_PATH, secure)
        return secure


def _apply_ego4d(values: dict[str, str]) -> None:
    access = values.get("access_key_id", "").strip()
    secret = values.get("secret_access_key", "").strip()
    session = values.get("session_token", "").strip()
    region = values.get("region", "").strip()
    os.environ["AWS_ACCESS_KEY_ID"] = access
    os.environ["AWS_SECRET_ACCESS_KEY"] = secret
    if session:
        os.environ["AWS_SESSION_TOKEN"] = session
    else:
        os.environ.pop("AWS_SESSION_TOKEN", None)
    os.environ["EGO4D_AWS_PROFILE"] = ""
    if region:
        os.environ["EGO4D_AWS_REGION"] = region
    else:
        os.environ.pop("EGO4D_AWS_REGION", None)
    config.EGO4D_AWS_PROFILE = ""
    config.EGO4D_AWS_REGION = region


def _apply_hostinger(values: dict[str, str]) -> None:
    token = values.get("token", "").strip()
    mailbox = values.get("mailbox_id", "").strip()
    os.environ["HOSTINGER_MAIL_TOKEN"] = token
    config.HOSTINGER_MAIL_TOKEN = token
    if mailbox:
        os.environ["HOSTINGER_MAILBOX_ID"] = mailbox
    else:
        os.environ.pop("HOSTINGER_MAILBOX_ID", None)
    config.HOSTINGER_MAILBOX_ID = mailbox


def _integration_error(exc: Exception, service: str) -> str:
    text = str(exc).lower()
    code = ""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = str((response.get("Error") or {}).get("Code") or "").lower()
    if any(term in code or term in text for term in (
            "expiredtoken", "requestexpired", "expired")):
        return "A credencial expirou. Renove o acesso e salve as novas chaves."
    if any(term in code or term in text for term in (
            "invalidaccesskeyid", "signaturedoesnotmatch", "invalid token")):
        return "A credencial informada não foi reconhecida. Confira os campos."
    if "accessdenied" in code or "access denied" in text or "forbidden" in text:
        return "A credencial existe, mas não possui acesso ao conteúdo solicitado."
    if any(term in text for term in (
            "timeout", "connection", "network", "dns", "name resolution")):
        return f"Não foi possível conectar à {service}. Confira a internet e tente novamente."
    return f"A {service} não aceitou a configuração informada."


def _test_ego4d(values: dict[str, str]) -> dict[str, Any]:
    import boto3
    client = boto3.client(
        "s3",
        region_name=values.get("region") or None,
        aws_access_key_id=values["access_key_id"],
        aws_secret_access_key=values["secret_access_key"],
        aws_session_token=values.get("session_token") or None,
    )
    response = client.get_object(
        Bucket=ego4d.MANIFEST_BUCKET,
        Key=ego4d.METADATA_KEY,
        Range="bytes=0-0",
    )
    body = response.get("Body")
    if body is not None:
        body.close()
    return {"ok": True, "message": "Acesso ao catálogo Ego4D confirmado."}


def _integration_snapshot() -> dict[str, Any]:
    secure = _migrate_legacy_integrations()
    ego = secure.get("ego4d") if isinstance(secure.get("ego4d"), dict) else {}
    host = (secure.get("hostinger")
            if isinstance(secure.get("hostinger"), dict) else {})
    if not ego:
        ego = _legacy_aws_credentials()
    ego_dir = config.MEDIA_DATA_DIR / "ego4d"
    holo_dir = config.MEDIA_DATA_DIR / "holoassist"
    seed_names = (
        "video.index.json.gz", "video_compress.index.json.gz", "imu.index.json.gz",
    )
    holo_indexes = all((holoassist._INDEX_SEED_DIR / name).is_file()
                       for name in seed_names)
    holo_catalog = (
        holoassist.annotations_path().is_file()
        and (holo_dir / "data-splits-v1_2.zip").is_file()
    )
    storage = _storage_snapshot(include_path=True)
    access = str(ego.get("access_key_id") or "")
    return {
        "security": {
            "provider": "Windows DPAPI",
            "detail": "Segredos criptografados para este usuário do Windows.",
        },
        "ego4d": {
            "configured": bool(access and ego.get("secret_access_key")),
            "access_hint": f"••••{access[-4:]}" if len(access) >= 4 else "",
            "region": str(ego.get("region") or config.EGO4D_AWS_REGION or "automática"),
            "catalog_ready": ((ego_dir / "ego4d.json").is_file()
                              and (ego_dir / "clips.csv").is_file()),
        },
        "hostinger": {
            "configured": bool(host.get("token") or config.HOSTINGER_MAIL_TOKEN),
            "mailbox_configured": bool(host.get("mailbox_id")
                                       or config.HOSTINGER_MAILBOX_ID),
        },
        "holoassist": {
            "catalog_ready": holo_catalog,
            "indexes_ready": holo_indexes,
        },
        "runtime": {
            "ffmpeg_ready": readiness._binary_works(readiness.ffmpeg_bin()),
            "ffprobe_ready": readiness._binary_works(readiness.ffprobe_bin()),
        },
        "library": storage,
    }


def _campaign_log_view(data: dict[str, Any]) -> dict[str, Any]:
    """Resumo operacional legível, sem IDs, caminhos ou respostas de API."""
    configured = [str(email) for email in data.get("accounts", []) if email]
    by_account: dict[str, dict[str, Any]] = {
        email: {"email": email, "success": 0, "failed": 0, "skipped": 0}
        for email in configured
    }
    items: list[dict[str, Any]] = []
    total_success = total_failed = total_skipped = 0
    for index, raw_item in enumerate(data.get("items", []), 1):
        if not isinstance(raw_item, dict):
            continue
        results: list[dict[str, Any]] = []
        for raw_result in raw_item.get("accounts", []):
            if not isinstance(raw_result, dict):
                continue
            email = str(raw_result.get("email") or "Conta")
            stats = by_account.setdefault(
                email, {"email": email, "success": 0, "failed": 0, "skipped": 0}
            )
            if raw_result.get("skipped"):
                status = "skipped"
                detail = (friendly_campaign_error(raw_result.get("error"))
                          if raw_result.get("error") else "Vídeo já processado anteriormente.")
                stats["skipped"] += 1
                total_skipped += 1
            elif raw_result.get("ok"):
                status = "success"
                detail = "Envio concluído."
                stats["success"] += 1
                total_success += 1
            else:
                status = "failed"
                detail = friendly_campaign_error(raw_result.get("error"))
                stats["failed"] += 1
                total_failed += 1
            results.append({"email": email, "status": status, "detail": detail})
        task = str(raw_item.get("task_name") or raw_item.get("task_scenario")
                   or raw_item.get("scenario") or f"Vídeo {index}")
        duration_s = max(0, int(float(raw_item.get("duration_ms") or 0) / 1000))
        items.append({
            "index": index,
            "task": task,
            "duration_s": duration_s,
            "success": sum(result["status"] == "success" for result in results),
            "failed": sum(result["status"] == "failed" for result in results),
            "skipped": sum(result["status"] == "skipped" for result in results),
            "accounts": results,
        })
    return {
        "schema": 2,
        "started_at": data.get("started_at"),
        "summary": {
            "configured_accounts": len(configured),
            "videos": len(items),
            "success": total_success,
            "failed": total_failed,
            "skipped": total_skipped,
        },
        "accounts": sorted(by_account.values(), key=lambda item: item["email"].lower()),
        "items": items,
    }


# --- contas -------------------------------------------------------------------

def _list_accounts() -> list[dict[str, Any]]:
    """Contas = token_*.json em secrets/ (sem rede). org_key vem do cache de prefs."""
    prefs = _load_prefs()
    org_keys = prefs.get("org_keys", {})
    out: list[dict[str, Any]] = []
    for path in sorted(config.tokens_dir().glob("token_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        email = data.get("email")
        if not email:
            continue
        out.append({
            "email": email,
            "expires_at": data.get("expires_at", 0),
            "org_key": org_keys.get(email),
        })
    return out


def _resolve_org(email: str, session: Session | None = None) -> str:
    """Resolve (e cacheia) a org_key da conta: prefs -> me()['organizations'][0]."""
    # Validar sempre, inclusive quando a organização já está no cache. O HUB
    # devolve /users/me = 200 para contas desativadas; ensure_auth inspeciona o
    # campo `disabled` e impede que a campanha comece com uma conta bloqueada.
    sess = session or Session.from_email(email)
    profile = sess.ensure_auth()
    cached = _load_prefs().get("org_keys", {}).get(email)
    if cached:
        return cached
    orgs = profile.get("organizations") or []
    if not orgs:
        raise RuntimeError(f"a conta {email} não pertence a nenhuma organização")
    org_key = orgs[0]["resourceKey"]
    # Outra resolução pode terminar ao mesmo tempo. Releia dentro do lock para
    # não sobrescrever a org_key que a thread vizinha acabou de persistir.
    with _PERSISTENCE_LOCK:
        prefs = _load_prefs()
        prefs.setdefault("org_keys", {})[email] = org_key
        _save_prefs(prefs)
    return org_key


def _check_account_health(email: str) -> dict[str, Any]:
    """Valida uma conta e devolve um estado estável para a verificação em lote."""
    try:
        session = Session.from_email(email)
        org_key = _resolve_org(email, session=session)
        return {
            "email": email, "status": "active", "org_key": org_key,
            "expires_at": session.data.get("expires_at", 0),
        }
    except (AuthError, RuntimeError, OSError) as exc:
        error = str(exc)
        disabled = "conta desativada no hub" in error.lower()
        return {
            "email": email,
            "status": "disabled" if disabled else "error",
            "error": error,
        }


# --- saldos (crowtado) ----------------------------------------------------------

def _load_balances() -> dict[str, Any]:
    """Cache de saldos: {email: {availableCents, ..., updated_at, error?}}."""
    value = load_json(BALANCES_PATH, {})
    return value if isinstance(value, dict) else {}


def _save_balances(balances: dict[str, Any]) -> None:
    with _PERSISTENCE_LOCK:
        save_json(BALANCES_PATH, balances)


def _crowtado_creds() -> dict[str, str]:
    """Senhas do crowtado por email: contas.jsonl (registrar_conta) + arquivo de senhas."""
    creds: dict[str, str] = {}
    contas = config.DATA_DIR / "contas.jsonl"
    try:
        for line in contas.read_text(encoding="utf-8-sig").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("email") and rec.get("senha"):
                creds[rec["email"]] = rec["senha"]
    except OSError:
        pass
    stored = load_json(CROWTADO_PW_PATH, {})
    if isinstance(stored, dict):
        creds.update({str(email): str(password)
                      for email, password in stored.items()})
    return creds


def _save_crowtado_cred(email: str, password: str) -> None:
    with _PERSISTENCE_LOCK:
        stored = load_json(CROWTADO_PW_PATH, {})
        creds = stored if isinstance(stored, dict) else {}
        creds[email] = password
        save_json(CROWTADO_PW_PATH, creds)


def _remove_account_data(email: str) -> None:
    """Remove caches editáveis ligados à conta (o cadastro histórico fica intacto)."""
    with _PERSISTENCE_LOCK:
        stored = load_json(CROWTADO_PW_PATH, {})
        creds = stored if isinstance(stored, dict) else {}
        if creds.pop(email, None) is not None:
            save_json(CROWTADO_PW_PATH, creds)

        balances = _load_balances()
        if balances.pop(email, None) is not None:
            _save_balances(balances)
    crowtado.clear_cached_session(email)


def _on_balance_result(email: str, summary: dict | None, erro: str | None) -> None:
    with _PERSISTENCE_LOCK:
        balances = _load_balances()
        rec = {"updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        if summary:
            rec.update(summary)
            rec["error"] = None
        else:
            rec["error"] = erro
        balances[email] = rec
        _save_balances(balances)


def _withdraw_message(email: str, result: dict[str, Any]) -> str:
    """Mensagem curta e segura para o resultado do payouts.withdraw."""
    status = str(result.get("status") or "")
    if status == "ok":
        if result.get("dotsEmailDelivery") == "sent":
            return f"link de saque enviado por email para {email}"
        if result.get("dotsSmsDelivery") == "sent":
            return f"link de saque enviado por SMS para a conta {email}"
        if result.get("dotsEmailDelivery") == "already_settled":
            return f"saque solicitado para {email}; cadastro Dots já estava concluído"
        return f"solicitação de saque enviada para {email}"
    if status == "review_required":
        return f"saque de {email} enviado para revisão do Crowtado"
    messages = {
        "below_minimum": "saldo abaixo do mínimo para saque",
        "hold": "saques estão temporariamente bloqueados para esta conta",
        "dots_not_ready": "o método Dots ainda não está disponível para esta conta",
        "no_balance": "não há saldo disponível para saque",
        "dots_failed": "o Dots recusou a solicitação de saque",
        "dots_pending_retry": "o Crowtado ainda tentará enviar o link novamente",
    }
    return messages.get(
        status, f"saque não solicitado (status: {status or 'desconhecido'})")


# --- app -----------------------------------------------------------------------

def _parse_duration_range(values) -> tuple[float, float]:
    """Valida o teto da UI e o mínimo legado de clientes anteriores."""
    bounds = []
    for name, default in (("min_dur_s", 60), ("max_dur_s", MAX_DUR_S)):
        try:
            value = float(values.get(name, default))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{name} inválido") from exc
        if not math.isfinite(value) or not 60 <= value <= MAX_DUR_S:
            raise ValueError(f"{name} deve estar entre 60 e 1800 segundos")
        bounds.append(value)
    minimum, maximum = bounds
    if minimum > maximum:
        raise ValueError("min_dur_s não pode exceder max_dur_s")
    return minimum, maximum


def create_app() -> Flask:
    # No QMoney o Flask e apenas o servico local consumido pela interface Qt.
    # Nenhum frontend web e publicado ou usado como fallback.
    app = Flask(__name__, static_folder=None)

    @app.get("/")
    def index():
        return jsonify({"service": "qmoney", "ui": "qt", "ok": True})

    # -- contas ---------------------------------------------------------------
    @app.get("/api/accounts")
    def get_accounts():
        return jsonify({"accounts": _list_accounts()})

    @app.post("/api/accounts")
    def add_account():
        body = request.get_json(silent=True) or {}
        email = str(body.get("email", "")).strip()
        password = str(body.get("password", ""))
        if not email or not password:
            return jsonify({"error": "informe email e senha"}), 400
        try:
            login(email, password)
        except (RuntimeError, OSError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "email": email})

    @app.post("/api/accounts/register")
    def register_account():
        """Cria conta nova no Minute (convite fixo) e já deixa tudo salvo.

        Se o email já existir no Minute, cai pro login simples. A senha também
        vai para secrets/crowtado_passwords.json (mesma credencial do crowtado)
        para a aba Saldos funcionar sem redigitar.
        """
        from ..minute_api import register as minute_register

        body = request.get_json(silent=True) or {}
        email = str(body.get("email", "")).strip()
        password = str(body.get("password", ""))
        if not email or not password:
            return jsonify({"error": "informe email e senha"}), 400
        try:
            minute_register(email, password)  # usa config.INVITE_CODE
            created = True
        except RuntimeError:
            try:
                login(email, password)  # já existia — só autentica
                created = False
            except RuntimeError as exc:
                return jsonify({"error": str(exc)}), 400
        _save_crowtado_cred(email, password)
        return jsonify({"ok": True, "email": email, "created": created})

    @app.delete("/api/accounts/<email>")
    def remove_account(email: str):
        if RUNNER.running:
            return jsonify({"error": "pare a campanha antes de remover uma conta"}), 409
        if BALANCES_RUNNER.running:
            return jsonify({"error": "aguarde a consulta de saldos terminar"}), 409
        path = config.token_path(email)
        if not path.exists():
            return jsonify({"error": f"conta não encontrada: {email}"}), 404
        path.unlink()
        prefs = _load_prefs()
        prefs.get("org_keys", {}).pop(email, None)
        selected = prefs.get("selected_accounts")
        if isinstance(selected, list):
            prefs["selected_accounts"] = [e for e in selected if e != email]
        _save_prefs(prefs)
        _remove_account_data(email)
        return jsonify({"ok": True})

    @app.post("/api/accounts/<email>/check")
    def check_account(email: str):
        try:
            sess = Session.from_email(email)
            org_key = _resolve_org(email, session=sess)
        except (AuthError, RuntimeError, OSError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "email": email, "org_key": org_key,
                        "expires_at": sess.data.get("expires_at", 0)})

    @app.post("/api/accounts/check-all")
    def check_all_accounts():
        if RUNNER.running:
            return jsonify({
                "error": "aguarde ou pare a campanha antes de verificar todas as contas",
            }), 409
        emails = [account["email"] for account in _list_accounts()]
        results_by_email: dict[str, dict[str, Any]] = {}
        if emails:
            workers = min(6, len(emails))
            with ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix="moneymin-account-check") as pool:
                futures = {pool.submit(_check_account_health, email): email
                           for email in emails}
                for future in as_completed(futures):
                    email = futures[future]
                    try:
                        results_by_email[email] = future.result()
                    except Exception as exc:  # noqa: BLE001 — uma conta não mata o lote
                        results_by_email[email] = {
                            "email": email, "status": "error",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
        results = [results_by_email[email] for email in emails]
        return jsonify({
            "ok": True,
            "total": len(results),
            "active": sum(result["status"] == "active" for result in results),
            "disabled": [result for result in results
                         if result["status"] == "disabled"],
            "errors": [result for result in results if result["status"] == "error"],
            "results": results,
        })

    # -- categorias --------------------------------------------------------------
    @app.get("/api/tasks")
    def get_tasks():
        email = str(request.args.get("email", "")).strip()
        if not email:
            return jsonify({"error": "informe ?email=<conta>"}), 400
        try:
            dataset_provider = campaign.normalize_dataset_provider(
                request.args.get("dataset")
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        try:
            min_dur_s, max_dur_s = _parse_duration_range(request.args)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        try:
            # Uma Session só: _resolve_org + catálogo. Dois refresh seguidos
            # no Firebase invalidam o refreshToken e o GET vira 400.
            sess = Session.from_email(email)
            org_key = _resolve_org(email, session=sess)
            tasks = campaign.available_tasks(
                email, org_key, min_dur_s=min_dur_s, max_dur_s=max_dur_s,
                include_unavailable=True, dataset_provider=dataset_provider,
                session=sess)
        except json.JSONDecodeError:
            return jsonify({
                "error": "a API devolveu resposta vazia (não-JSON). Tente de novo.",
            }), 400
        except AuthError as exc:
            app.logger.warning("GET /api/tasks auth %s: %s", email, exc)
            return jsonify({"error": str(exc)}), 400
        except (RuntimeError, OSError) as exc:
            app.logger.warning("GET /api/tasks %s: %s", email, exc)
            msg = str(exc)
            if "Expecting value" in msg:
                msg = "a API devolveu resposta vazia (não-JSON). Tente de novo."
            return jsonify({"error": msg}), 400
        return jsonify({"email": email, "org_key": org_key,
                        "dataset": dataset_provider, "tasks": tasks,
                        "scenarios_pt": campaign.SCENARIO_PT})

    # -- preferências -------------------------------------------------------------
    @app.get("/api/preferences")
    def get_prefs():
        return jsonify(_load_prefs())

    @app.put("/api/preferences")
    def put_prefs():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "body JSON inválido"}), 400
        with _PERSISTENCE_LOCK:
            prefs = _load_prefs()
            prefs.update(body)
            _save_prefs(prefs)
        return jsonify(prefs)

    @app.get("/api/integrations")
    def get_integrations():
        """Estados e dicas somente; nunca devolve um segredo salvo."""
        try:
            return jsonify(_integration_snapshot())
        except (OSError, RuntimeError, ValueError) as exc:
            return jsonify({"error": _integration_error(exc, "configuração local")}), 400

    @app.put("/api/integrations/ego4d")
    def put_ego4d_integration():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "preencha as credenciais do Ego4D"}), 400
        secure = _migrate_legacy_integrations()
        current = (secure.get("ego4d")
                   if isinstance(secure.get("ego4d"), dict)
                   else _legacy_aws_credentials())
        values = {
            "access_key_id": str(body.get("access_key_id") or current.get("access_key_id") or "").strip(),
            "secret_access_key": str(body.get("secret_access_key") or current.get("secret_access_key") or "").strip(),
            "session_token": str(
                body.get("session_token") if "session_token" in body
                else current.get("session_token") or "").strip(),
            "region": str(
                body.get("region") if "region" in body
                else current.get("region") or "").strip(),
        }
        if len(values["access_key_id"]) < 12 or len(values["secret_access_key"]) < 20:
            return jsonify({
                "error": "informe o Access Key ID e o Secret Access Key recebidos do Ego4D",
            }), 400
        try:
            tested = _test_ego4d(values)
        except Exception as exc:  # noqa: BLE001 — traduz resposta de boto/AWS
            return jsonify({"error": _integration_error(exc, "AWS do Ego4D")}), 400
        with _PERSISTENCE_LOCK:
            secure["schema"] = 1
            secure["ego4d"] = values
            save_secure_settings(config.INTEGRATIONS_PATH, secure)
            _apply_ego4d(values)
        return jsonify({"ok": True, "test": tested,
                        "integrations": _integration_snapshot()})

    @app.post("/api/integrations/ego4d/test")
    def test_ego4d_integration():
        secure = _migrate_legacy_integrations()
        current = (secure.get("ego4d")
                   if isinstance(secure.get("ego4d"), dict)
                   else _legacy_aws_credentials())
        body = request.get_json(silent=True) or {}
        values = {
            "access_key_id": str(body.get("access_key_id") or current.get("access_key_id") or "").strip(),
            "secret_access_key": str(body.get("secret_access_key") or current.get("secret_access_key") or "").strip(),
            "session_token": str(body.get("session_token") or current.get("session_token") or "").strip(),
            "region": str(body.get("region") or current.get("region") or "").strip(),
        }
        if not values or not values.get("access_key_id") or not values.get("secret_access_key"):
            return jsonify({"error": "configure as credenciais do Ego4D primeiro"}), 400
        try:
            tested = _test_ego4d(values)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": _integration_error(exc, "AWS do Ego4D")}), 400
        return jsonify(tested)

    @app.post("/api/integrations/ego4d/catalog")
    def prepare_ego4d_catalog():
        secure = _migrate_legacy_integrations()
        values = (secure.get("ego4d")
                  if isinstance(secure.get("ego4d"), dict)
                  else _legacy_aws_credentials())
        if not values or not values.get("access_key_id") or not values.get("secret_access_key"):
            return jsonify({"error": "configure as credenciais do Ego4D primeiro"}), 400
        _apply_ego4d(values)
        try:
            meta, clips = ego4d.sync_meta()
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": _integration_error(exc, "AWS do Ego4D")}), 400
        return jsonify({
            "ok": True,
            "message": "Catálogo básico do Ego4D preparado.",
            "metadata_bytes": meta.stat().st_size,
            "clips_bytes": clips.stat().st_size,
        })

    @app.put("/api/integrations/hostinger")
    def put_hostinger_integration():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "informe o token da Hostinger"}), 400
        secure = _migrate_legacy_integrations()
        current = (secure.get("hostinger")
                   if isinstance(secure.get("hostinger"), dict) else {})
        values = {
            "token": str(body.get("token") or current.get("token") or "").strip(),
            "mailbox_id": str(
                body.get("mailbox_id") if "mailbox_id" in body
                else current.get("mailbox_id") or "").strip(),
        }
        if len(values["token"]) < 16:
            return jsonify({"error": "informe um token válido da API Mail da Hostinger"}), 400
        try:
            tested = hostinger_mail.test_connection(
                token=values["token"], mailbox=values["mailbox_id"] or None)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": _integration_error(exc, "Hostinger")}), 400
        with _PERSISTENCE_LOCK:
            secure["schema"] = 1
            secure["hostinger"] = values
            save_secure_settings(config.INTEGRATIONS_PATH, secure)
            _apply_hostinger(values)
        return jsonify({"ok": True, "test": tested,
                        "integrations": _integration_snapshot()})

    @app.post("/api/integrations/hostinger/test")
    def test_hostinger_integration():
        secure = _migrate_legacy_integrations()
        current = (secure.get("hostinger")
                   if isinstance(secure.get("hostinger"), dict) else {})
        body = request.get_json(silent=True) or {}
        token = str(body.get("token") or current.get("token")
                    or config.HOSTINGER_MAIL_TOKEN or "").strip()
        mailbox = str(body.get("mailbox_id") or current.get("mailbox_id")
                      or config.HOSTINGER_MAILBOX_ID or "").strip()
        if not token:
            return jsonify({"error": "configure o token da Hostinger primeiro"}), 400
        try:
            return jsonify(hostinger_mail.test_connection(
                token=token, mailbox=mailbox or None))
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": _integration_error(exc, "Hostinger")}), 400

    # -- prontidão, biblioteca e diagnóstico -----------------------------------
    @app.get("/api/readiness")
    def get_readiness():
        try:
            provider = campaign.normalize_dataset_provider(
                request.args.get("dataset")
            )
            result = readiness.campaign_readiness(provider)
        except (ValueError, OSError, RuntimeError) as exc:
            return jsonify({"error": str(exc)}), 400
        result["storage"] = _storage_snapshot(include_path=False)
        return jsonify(result)

    @app.get("/api/storage/library")
    def get_storage_library():
        return jsonify(_storage_snapshot(include_path=True))

    @app.get("/api/diagnostics")
    def get_diagnostics():
        """Relatório deliberadamente sem emails, tokens, senhas ou URLs privadas."""
        try:
            ready = readiness.campaign_readiness("all")
        except Exception as exc:  # noqa: BLE001 — diagnóstico precisa continuar
            ready = {
                "ready": False,
                "provider": "all",
                "checks": [{
                    "name": "Diagnóstico de prontidão",
                    "status": "error",
                    "detail": f"{type(exc).__name__}: {exc}",
                }],
            }
        campaign_state = RUNNER.snapshot()
        return jsonify({
            "schema": 1,
            "generated_at": int(time.time()),
            "service": {
                "app_version": os.environ.get("QMONEY_APP_VERSION", "unknown"),
                "python": platform.python_version(),
                "platform": platform.platform(),
                "frozen": bool(getattr(sys, "frozen", False)),
            },
            "accounts": {"configured": len(_list_accounts())},
            "readiness": ready,
            "storage": _storage_snapshot(include_path=False),
            "runners": {
                "campaign": {
                    "state": campaign_state.get("state"),
                    "totals": campaign_state.get("totals"),
                    "has_error": bool(campaign_state.get("error")),
                },
                "balances": {"state": BALANCES_RUNNER.state},
                "holo_cache": {"state": HOLO_CACHE_RUNNER.state},
            },
            "history": {"campaign_logs": len(campaign.list_campaign_logs())},
        })

    # -- acelerador HoloAssist -------------------------------------------------
    @app.get("/api/holo-cache")
    def holo_cache_status():
        task = str(request.args.get("task") or holo_accelerator.DEFAULT_TASK)
        if task not in holoassist.MINUTE_TASK_TYPES:
            return jsonify({"error": "tarefa HoloAssist inválida"}), 400
        raw_limit = request.args.get("limit")
        try:
            limit = None if raw_limit in (None, "", "0") else int(raw_limit)
            if limit is not None and not 1 <= limit <= 1000:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": "limit deve estar entre 1 e 1000"}), 400
        try:
            cache = holo_accelerator.cache_status(task, limit=limit)
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            cache = {
                "task": task,
                "total": 0,
                "ready": 0,
                "partial": 0,
                "pending": 0,
                "last_run": {},
                "catalog_error": str(exc),
            }
        return jsonify({
            "cache": cache,
            "runner": HOLO_CACHE_RUNNER.snapshot(),
            "tasks": sorted(holoassist.MINUTE_TASK_TYPES),
        })

    @app.post("/api/holo-cache/start")
    def holo_cache_start():
        body = request.get_json(silent=True) or {}
        task = str(body.get("task") or holo_accelerator.DEFAULT_TASK)
        if task not in holoassist.MINUTE_TASK_TYPES:
            return jsonify({"error": "tarefa HoloAssist inválida"}), 400
        try:
            raw_limit = body.get("limit")
            limit = None if raw_limit in (None, "", 0, "0") else int(raw_limit)
            if limit is not None and not 1 <= limit <= 1000:
                raise ValueError
            min_free_gb = float(body.get("min_free_gb", 50))
            if not math.isfinite(min_free_gb) or not 5 <= min_free_gb <= 1000:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({
                "error": "use limite entre 1 e 1000 e reserva de disco entre 5 e 1000 GiB",
            }), 400

        # Falhe antes de abrir a thread quando os metadados ainda não foram
        # instalados. A mensagem original explica qual comando deve ser usado.
        try:
            catalog = holo_accelerator.cache_status(task, limit=limit)
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            return jsonify({"error": str(exc)}), 400

        with _HEAVY_RUNNER_LOCK:
            if RUNNER.running:
                return jsonify({
                    "error": "pare a campanha antes de iniciar o acelerador HoloAssist",
                }), 409
            if HOLO_CACHE_RUNNER.running:
                return jsonify({
                    "ok": True,
                    "already_running": True,
                    "runner": HOLO_CACHE_RUNNER.snapshot(),
                })
            try:
                HOLO_CACHE_RUNNER.start(
                    task=task,
                    limit=limit,
                    min_free_gb=min_free_gb,
                )
            except RuntimeError as exc:
                return jsonify({"error": str(exc)}), 409
        return jsonify({
            "ok": True,
            "cache": catalog,
            "runner": HOLO_CACHE_RUNNER.snapshot(),
        })

    @app.post("/api/holo-cache/stop")
    def holo_cache_stop():
        HOLO_CACHE_RUNNER.stop()
        return jsonify({"ok": True, "runner": HOLO_CACHE_RUNNER.snapshot()})

    @app.post("/api/storage/cleanup")
    def storage_cleanup():
        """Limpeza manual confinada aos caches de mídia conhecidos."""
        with _HEAVY_RUNNER_LOCK:
            if RUNNER.running or HOLO_CACHE_RUNNER.running:
                return jsonify({
                    "error": "pare a campanha e o acelerador antes de limpar a mídia",
                }), 409
            result = campaign.cleanup_media_cache(config.MEDIA_DATA_DIR / "ego4d")
        return jsonify({"ok": not result["errors"], **result})

    # -- campanha ---------------------------------------------------------------
    @app.post("/api/campaigns/preflight")
    def campaign_preflight():
        """Valida a operação inteira sem baixar, preparar ou enviar mídia."""
        body = request.get_json(silent=True) or {}
        blockers: list[str] = []
        warnings: list[str] = []
        if RUNNER.running:
            blockers.append("já existe uma campanha em andamento")
        if HOLO_CACHE_RUNNER.running:
            blockers.append("o acelerador HoloAssist está em execução")
        try:
            provider = campaign.normalize_dataset_provider(body.get("dataset"))
            min_dur_s, max_dur_s = _parse_duration_range(body)
            count = max(1, min(int(body.get("count", 1)), 200))
            target_hours = max(0.0, min(float(body.get("target_hours") or 0), 12.0))
        except (TypeError, ValueError, OverflowError) as exc:
            return jsonify({"error": f"parâmetros inválidos: {exc}"}), 400

        emails = [str(e).strip() for e in body.get("accounts", []) if str(e).strip()]
        raw_tasks = body.get("tasks", [])
        if not emails:
            blockers.append("selecione ao menos uma conta")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            blockers.append("selecione ao menos uma categoria")
        known = {account["email"] for account in _list_accounts()}
        missing_accounts = [email for email in emails if email not in known]
        if missing_accounts:
            blockers.append("há contas sem token salvo")

        accounts: list[AccountSpec] = []
        account_errors: list[str] = []
        if emails and not missing_accounts:
            resolved: list[AccountSpec | None] = [None] * len(emails)
            with ThreadPoolExecutor(max_workers=max(1, min(6, len(emails)))) as pool:
                futures = {pool.submit(_resolve_org, email): i
                           for i, email in enumerate(emails)}
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        resolved[index] = AccountSpec(emails[index], future.result())
                    except (AuthError, RuntimeError, OSError) as exc:
                        account_errors.append(f"{emails[index]}: {exc}")
            accounts = [account for account in resolved if account is not None]
            if account_errors:
                blockers.append(f"{len(account_errors)} conta(s) não autenticaram")

        selected: list[dict[str, Any]] = []
        unavailable: list[str] = []
        if accounts and raw_tasks:
            try:
                catalog = campaign.available_tasks(
                    accounts[0].email, accounts[0].org_key,
                    min_dur_s=min_dur_s, max_dur_s=max_dur_s,
                    include_unavailable=True, dataset_provider=provider)
            except (AuthError, RuntimeError, OSError, json.JSONDecodeError) as exc:
                blockers.append(f"catálogo indisponível: {exc}")
                catalog = []
            by_id = {str(item.get("id")): item for item in catalog if item.get("id")}
            seen: set[str] = set()
            for raw in raw_tasks:
                task_id = str(raw.get("task_id", "")).strip() if isinstance(raw, dict) else ""
                item = by_id.get(task_id)
                if not task_id or item is None:
                    blockers.append("uma categoria selecionada não está mais disponível")
                    continue
                if task_id in seen:
                    continue
                seen.add(task_id)
                label = str(item.get("name_pt") or item.get("name") or task_id)
                if item.get("available_for_duration") is False:
                    unavailable.append(label)
                    continue
                selected.append(item)

            selected_ids = {str(item.get("id")) for item in selected}
            for account in accounts[1:]:
                try:
                    sess = Session.from_email(account.email)
                    sess.ensure_auth()
                    account_ids = {
                        str(item.get("id")) for item in sess.all_tasks(account.org_key)
                        if item.get("id")
                    }
                except (AuthError, RuntimeError, OSError) as exc:
                    blockers.append(f"preflight falhou para {account.email}: {exc}")
                    continue
                missing = selected_ids - account_ids
                if missing:
                    blockers.append(
                        f"{account.email} não possui {len(missing)} categoria(s) selecionada(s)"
                    )

        if unavailable:
            warnings.append(f"{len(unavailable)} categoria(s) sem clipe na duração escolhida")
        if raw_tasks and not selected:
            blockers.append("nenhuma categoria selecionada possui clipe compatível")

        clip_count = sum(int(item.get("clip_count") or 0) for item in selected)
        if target_hours > 0:
            estimated_sends = max(1, math.ceil(target_hours * 4)) * len(accounts)
        else:
            estimated_sends = len(selected) * count * len(accounts)
        try:
            ready = readiness.campaign_readiness(provider)
        except Exception as exc:  # noqa: BLE001 — ainda devolve os outros checks
            ready = {"ready": False, "checks": [], "error": str(exc)}
        storage = _storage_snapshot(include_path=False)
        if storage.get("free_bytes", 0) < 10 * 1024 ** 3:
            warnings.append("há menos de 10 GiB livres na unidade da biblioteca")

        return jsonify({
            "ok": not blockers,
            "provider": provider,
            "accounts": {"selected": len(emails), "validated": len(accounts)},
            "tasks": {"selected": len(raw_tasks), "compatible": len(selected)},
            "clips": clip_count,
            "estimated_sends": estimated_sends,
            "target_hours": target_hours,
            "blockers": blockers,
            "warnings": warnings,
            "account_errors": account_errors,
            "readiness": ready,
            "storage": storage,
        }), 200

    @app.post("/api/campaigns")
    def start_campaign():
        if RUNNER.running:
            return jsonify({
                "ok": True,
                "already_running": True,
                "state": RUNNER.state,
                "total_sends": RUNNER.total_sends,
            })
        if HOLO_CACHE_RUNNER.running:
            return jsonify({
                "error": "pare o acelerador HoloAssist antes de iniciar a campanha",
            }), 409
        body = request.get_json(silent=True) or {}
        cleanup_after_upload = body.get("cleanup_after_upload", True)
        if not isinstance(cleanup_after_upload, bool):
            return jsonify({
                "error": "cleanup_after_upload deve ser true ou false",
            }), 400
        try:
            dataset_provider = campaign.normalize_dataset_provider(
                body.get("dataset")
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        emails = [str(e).strip() for e in body.get("accounts", []) if str(e).strip()]
        raw_tasks = body.get("tasks", [])
        if not emails:
            return jsonify({"error": "selecione ao menos uma conta"}), 400
        if not raw_tasks:
            return jsonify({"error": "selecione ao menos uma categoria"}), 400
        try:
            count = max(1, min(int(body.get("count", 1)), 200))
            if "target_hours" in body:
                target_hours = max(0.0, min(float(body.get("target_hours") or 0), 12.0))
            else:
                target_hours = 0.0
            min_dur_s, max_dur_s = _parse_duration_range(body)
            raw_delay_s = float(body.get("delay_s", 0))
            # Campo antigo ainda é validado para clientes desatualizados.
            raw_account_gap_s = float(body.get("account_gap_s", 300))
            if not all(math.isfinite(v) for v in (
                    raw_delay_s, raw_account_gap_s, target_hours)):
                raise ValueError("valor não finito")
            delay_s = max(0.0, min(raw_delay_s, 3600.0))
        except (TypeError, ValueError, OverflowError) as exc:
            return jsonify({"error": f"parâmetros numéricos inválidos: {exc}"}), 400
        delay_mode = str(body.get("delay_mode", "off"))
        if delay_mode not in ("off", "clip", "fixed"):
            return jsonify({"error": "delay_mode inválido (off|clip|fixed)"}), 400
        active_hours = _parse_active_hours(body.get("active_hours"))
        if active_hours is False:
            return jsonify({"error": "active_hours inválido — use [início, fim] "
                            "com 0 <= início < fim <= 24"}), 400

        known = {a["email"] for a in _list_accounts()}
        missing = [e for e in emails if e not in known]
        if missing:
            return jsonify({"error": "conta(s) sem token: " + ", ".join(missing)}), 400

        resolved: list[AccountSpec | None] = [None] * len(emails)
        skipped: list[str] = []
        workers = max(1, min(8, len(emails)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_resolve_org, email): i
                    for i, email in enumerate(emails)}
            for fut in as_completed(futs):
                i = futs[fut]
                try:
                    resolved[i] = AccountSpec(emails[i], fut.result())
                except (AuthError, RuntimeError, OSError) as exc:
                    skipped.append(f"{emails[i]}: {exc}")
        accounts = [acc for acc in resolved if acc is not None]
        if not accounts:
            return jsonify({
                "error": "nenhuma conta autenticou. " + "; ".join(skipped),
            }), 400

        # Nunca aceite do browser a associação task_id -> cenário. Uma aba
        # antiga ou uma troca rápida de conta podia mandar um par inconsistente
        # e selecionar vídeos de outra categoria. O catálogo atual da conta é a
        # fonte de verdade.
        try:
            available = campaign.available_tasks(
                accounts[0].email, accounts[0].org_key,
                min_dur_s=min_dur_s, max_dur_s=max_dur_s,
                include_unavailable=True, dataset_provider=dataset_provider)
        except json.JSONDecodeError:
            return jsonify({
                "error": "a API devolveu resposta vazia (não-JSON). Tente de novo.",
            }), 400
        except (AuthError, RuntimeError, OSError) as exc:
            msg = str(exc)
            if "Expecting value" in msg:
                msg = "a API devolveu resposta vazia (não-JSON). Tente de novo."
            return jsonify({"error": msg}), 400
        by_id = {str(t.get("id")): t for t in available if t.get("id")}

        tasks: list[TaskSpec] = []
        seen_ids: set[str] = set()
        for t in raw_tasks:
            if not isinstance(t, dict):
                return jsonify({"error": "categoria inválida"}), 400
            task_id = str(t.get("task_id", "")).strip()
            authoritative = by_id.get(task_id)
            if not task_id or authoritative is None:
                return jsonify({"error": f"categoria indisponível: {task_id or '(sem id)'}"}), 400
            if task_id in seen_ids:
                continue
            seen_ids.add(task_id)
            # Sem footage compatível (higiene / duração): ignora, não aborta o lote.
            if authoritative.get("available_for_duration") is False:
                skipped.append(str(authoritative.get("name_pt")
                                   or authoritative.get("name")
                                   or task_id))
                continue
            scenario = str(authoritative["scenario"])
            task_name = str(authoritative.get("name") or scenario)
            task_label = str(authoritative.get("name_pt") or task_name)
            task_description = str(authoritative.get("description") or "")
            tasks.append(TaskSpec(task_id=task_id, scenario=scenario,
                                  min_dur_s=min_dur_s, max_dur_s=max_dur_s,
                                  task_name=task_name, task_label=task_label,
                                  task_description=task_description,
                                  count=count))

        if not tasks:
            return jsonify({
                "error": "nenhuma categoria selecionada tem clipe compatível "
                         "(sentado/celular/título). " + (
                             ("Fora: " + ", ".join(skipped)) if skipped else ""),
            }), 400

        # Preflight estrito nas DEMAIS contas. A primeira já foi autenticada e
        # forneceu o catálogo autoritativo acima; todas as outras precisam ter
        # as mesmas tasks antes de qualquer download/upload começar.
        selected_ids = {task.task_id for task in tasks}

        def _preflight(account: AccountSpec) -> tuple[str, set[str] | str]:
            try:
                sess = Session.from_email(account.email)
                sess.ensure_auth()
                account_task_ids = {
                    str(task.get("id")) for task in sess.all_tasks(account.org_key)
                    if task.get("id")
                }
            except (AuthError, RuntimeError, OSError) as exc:
                return account.email, f"preflight falhou para {account.email}: {exc}"
            missing_tasks = selected_ids - account_task_ids
            return account.email, missing_tasks

        others = accounts[1:]
        if others:
            by_email = {acc.email: acc for acc in accounts}
            viable = [accounts[0]]
            workers = max(1, min(8, len(others)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for email, result in pool.map(_preflight, others):
                    if isinstance(result, str):
                        skipped.append(result)
                        continue
                    if result:
                        labels = [task.task_label or task.task_name or task.task_id
                                  for task in tasks if task.task_id in result]
                        return jsonify({
                            "error": (f"{email} não possui as categorias "
                                      f"selecionadas: {', '.join(labels)}. "
                                      "A campanha não foi iniciada.")
                        }), 400
                    viable.append(by_email[email])
            accounts = viable
            if not accounts:
                return jsonify({
                    "error": "nenhuma conta passou no preflight. "
                             + "; ".join(skipped),
                }), 400

        # O MP4 é normalizado uma vez antes do lote. Três PUTs simultâneos
        # reduzem 11 contas a quatro ondas sem disputar CPU com o ffmpeg.
        account_workers = min(len(accounts), campaign.max_account_workers())

        cfg = CampaignConfig(accounts=accounts, tasks=tasks,
                             work_dir=config.MEDIA_DATA_DIR / "ego4d",
                             timeout_blob=campaign.DEFAULT_TIMEOUT_BLOB,
                             evaluate=True, finalize=True,
                             delay_mode=delay_mode, delay_s=delay_s,
                             account_gap_s=campaign.DEFAULT_ACCOUNT_STAGGER_S,
                             account_workers=account_workers,
                             account_max_attempts=5,
                             account_retry_s=15,
                             require_all_accounts=False,
                             share_clips=True,
                             unique_video=False,
                             allow_new_accounts=False,
                             target_hours_per_account=target_hours,
                             dataset_provider=dataset_provider,
                             cleanup_after_upload=cleanup_after_upload,
                             realistic_timeline=True,
                             active_hours=active_hours)
        with _HEAVY_RUNNER_LOCK:
            if HOLO_CACHE_RUNNER.running:
                return jsonify({
                    "error": "pare o acelerador HoloAssist antes de iniciar a campanha",
                }), 409
            try:
                RUNNER.start(cfg)
            except RuntimeError as exc:
                # Corrida entre dois cliques/abas: se o outro request venceu e
                # iniciou, este POST também é sucesso idempotente, nunca erro 409.
                if RUNNER.running:
                    return jsonify({
                        "ok": True,
                        "already_running": True,
                        "state": RUNNER.state,
                        "total_sends": RUNNER.total_sends,
                    })
                return jsonify({"error": str(exc)}), 409
        payload = {
            "ok": True,
            "total_sends": RUNNER.total_sends,
            "selected_tasks": [
                {"task_id": t.task_id, "scenario": t.scenario} for t in tasks
            ],
            "accounts": [acc.email for acc in accounts],
            "dataset": dataset_provider,
        }
        if skipped:
            payload["skipped_accounts"] = skipped
        return jsonify(payload)

    @app.get("/api/campaigns/current")
    def campaign_current():
        try:
            since = int(request.args.get("since", 0))
        except ValueError:
            since = 0
        return jsonify(RUNNER.snapshot(since=since))

    @app.post("/api/campaigns/stop")
    def campaign_stop():
        RUNNER.stop()
        return jsonify({"ok": True, "state": RUNNER.state})

    # -- histórico ----------------------------------------------------------------
    @app.get("/api/logs")
    def get_logs():
        out: list[dict[str, Any]] = []
        for path in campaign.list_campaign_logs():
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            items = data.get("items", [])
            sends = [acc for it in items for acc in it.get("accounts", [])
                     if not acc.get("skipped")]
            out.append({
                "name": path.name,
                "started_at": data.get("started_at"),
                "accounts": data.get("accounts", []),
                "items": len(items),
                "sends": len(sends),
                "ok": sum(1 for s in sends if s.get("ok")),
            })
        return jsonify({"logs": out})

    @app.get("/api/logs/<name>")
    def get_log(name: str):
        path = _log_path(name)
        if path is None:
            return jsonify({"error": "log não encontrado"}), 404
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            return jsonify(_campaign_log_view(raw))
        except json.JSONDecodeError:
            return jsonify({"error": "log ilegível (JSON vazio/corrompido)"}), 400

    @app.post("/api/logs/<name>/status")
    def get_log_status(name: str):
        path = _log_path(name)
        if path is None:
            return jsonify({"error": "log não encontrado"}), 404
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        results: list[dict[str, Any]] = []
        for it in data.get("items", []):
            for acc in it.get("accounts", []):
                sid = acc.get("session_id")
                if not sid:
                    continue
                org = acc.get("org_key") or _safe_org(acc.get("email", ""))
                if not org:
                    results.append({"session_id": sid, "email": acc.get("email"),
                                    "status": "sem org_key"})
                    continue
                try:
                    results.append(campaign.session_result(acc["email"], org, sid))
                except (AuthError, RuntimeError, OSError) as exc:
                    results.append({"session_id": sid, "email": acc.get("email"),
                                    "status": f"erro: {exc}"})
        return jsonify({"results": results})

    # -- saldos (crowtado) -----------------------------------------------------
    @app.get("/api/balances")
    def get_balances():
        configured = sorted(a["email"] for a in _list_accounts())
        return jsonify({
            "balances": _load_balances(),
            "accounts": configured,
            "with_password": sorted(_crowtado_creds()),
            "runner": BALANCES_RUNNER.snapshot(),
        })

    @app.post("/api/balances/refresh")
    def refresh_balances():
        body = request.get_json(silent=True) or {}
        configured = {a["email"] for a in _list_accounts()}
        creds = {e: p for e, p in _crowtado_creds().items() if e in configured}
        emails = [str(e).strip() for e in body.get("emails", []) if str(e).strip()]
        if emails:
            creds = {e: creds[e] for e in emails if e in creds}
        if not creds:
            return jsonify({"error": "nenhuma conta configurada com senha do crowtado salva"}), 400
        try:
            BALANCES_RUNNER.start(creds, _on_balance_result)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"ok": True, "total": len(creds)})

    @app.post("/api/balances/withdraw")
    def request_balance_withdraw():
        """Solicita somente o envio do link Dots; conclusão e 2FA são manuais."""
        body = request.get_json(silent=True) or {}
        email = str(body.get("email", "")).strip()
        if not email:
            return jsonify({"error": "informe a conta"}), 400
        configured = {a["email"] for a in _list_accounts()}
        if email not in configured:
            return jsonify({"error": "conta não está configurada"}), 404
        password = _crowtado_creds().get(email)
        if not password:
            return jsonify({"error": "salve a senha do crowtado primeiro"}), 400
        if BALANCES_RUNNER.running:
            return jsonify({"error": "aguarde a consulta de saldos terminar"}), 409

        now = time.monotonic()
        with _WITHDRAW_LOCK:
            if email in _WITHDRAW_IN_FLIGHT:
                return jsonify({"error": "já há uma solicitação em andamento"}), 409
            elapsed = now - _WITHDRAW_LAST_REQUEST.get(email, 0.0)
            if elapsed < _WITHDRAW_COOLDOWN_S:
                wait_s = max(1, math.ceil(_WITHDRAW_COOLDOWN_S - elapsed))
                return jsonify({
                    "error": f"link já solicitado; aguarde {wait_s}s para repetir",
                }), 429
            _WITHDRAW_IN_FLIGHT.add(email)

        success = False
        try:
            result = crowtado.solicitar_link_saque(email, password)
            success = result.get("status") in {"ok", "review_required"}
            message = _withdraw_message(email, result)
            response = {
                "ok": success, "email": email,
                "message": message, "result": result,
            }
            return jsonify(response), 200 if success else 409
        except crowtado.CrowtadoError as exc:
            return jsonify({"error": str(exc)}), 400
        finally:
            with _WITHDRAW_LOCK:
                _WITHDRAW_IN_FLIGHT.discard(email)
                if success:
                    _WITHDRAW_LAST_REQUEST[email] = time.monotonic()

    @app.put("/api/balances/credentials")
    def put_balance_credentials():
        body = request.get_json(silent=True) or {}
        email = str(body.get("email", "")).strip()
        password = str(body.get("password", ""))
        if not email or not password:
            return jsonify({"error": "informe email e senha"}), 400
        _save_crowtado_cred(email, password)
        return jsonify({"ok": True, "email": email})

    # -- registro de enviados ---------------------------------------------------
    @app.get("/api/sent")
    def get_sent():
        return jsonify({"sent": sent_registry.summary()})

    @app.post("/api/sent/reset")
    def reset_sent():
        body = request.get_json(silent=True) or {}
        scenario = body.get("scenario")
        sent_registry.reset(str(scenario) if scenario else None)
        return jsonify({"ok": True, "sent": sent_registry.summary()})

    return app


def _log_path(name: str) -> Path | None:
    """Resolve um nome de log de forma segura (sem path traversal)."""
    if not (name.startswith("campaign_") and name.endswith(".json")) or "/" in name \
            or "\\" in name or name == "campaign.example.json":
        return None
    path = config.DATA_DIR / name
    return path if path.exists() else None


def _parse_active_hours(raw) -> tuple[int, int] | None | bool:
    """[7, 18] -> (7, 18); None/ausente -> None (sem janela); inválido -> False."""
    if raw is None:
        return None
    try:
        start, end = int(raw[0]), int(raw[1])
    except (TypeError, ValueError, IndexError):
        return False
    if not (0 <= start < end <= 24):
        return False
    return (start, end)


def _safe_org(email: str) -> str | None:
    try:
        return _resolve_org(email)
    except Exception:  # noqa: BLE001 — best-effort
        return None


__all__ = ["create_app"]
