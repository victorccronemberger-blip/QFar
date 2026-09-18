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
                                         body: {accounts, tasks, count,
                                         min_dur_s, max_dur_s,
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
import hashlib
import json
import math
import os
import platform
import random
import shutil
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from flask import Flask, g, jsonify, request

from .. import (
    campaign, config, crowtado, ego4d, fx, holo_accelerator, holoassist,
    hostinger_mail, identity, org_policy, readiness, sent_registry,
)
from ..atomic_io import load_json, save_json
from .. import account_transfer, account_bans
from ..campaign import AccountSpec, CampaignConfig, TaskSpec
from ..minute_api import AuthError, Session, login
from ..secure_store import load_secure_settings, save_secure_settings
from .account_issues import account_issue, issue_text
from .org_migration import OrgMigrationRunner
from .banned_monitor import BannedMonitor
from .runner import (
    BALANCES_RUNNER, HOLO_CACHE_RUNNER, RUNNER, friendly_campaign_error, _public_event,
)

PREFS_PATH = config.DATA_DIR / "webui_prefs.json"
BALANCES_PATH = config.DATA_DIR / "balances.json"
CROWTADO_PW_PATH = config.SECRETS_DIR / "crowtado_passwords.json"
MAX_DUR_S = 1800.0  # cap do recording-config do Minute
_PERSISTENCE_LOCK = threading.RLock()
_INTEGRATION_OPERATION_LOCK = threading.Lock()
_ACCOUNT_OPERATION_LOCK = threading.Lock()
ORG_MIGRATION = OrgMigrationRunner(config.DATA_DIR / "org_migration_report.json")
ACCOUNT_HEALTH_PATH = config.DATA_DIR / "account_health.json"
_BULK_REGISTER_LOCK = threading.Lock()
_BULK_REGISTER_STATE: dict[str, Any] = {"state": "idle"}
_HEAVY_RUNNER_LOCK = threading.Lock()
_WITHDRAW_LOCK = threading.Lock()
_WITHDRAW_IN_FLIGHT: set[str] = set()
_WITHDRAW_LAST_REQUEST: dict[str, float] = {}
_WITHDRAW_COOLDOWN_S = 60.0

_STEP_LABELS = {
    "ban_check": "verificação de ban",
    "crowtado_signup": "criação Crowtado",
    "save_partial": "salvar credenciais parciais",
    "demographics": "demografia Crowtado",
    "minute_register": "registro Minute",
    "link_minute": "vincular Minute na Crowtado",
    "validate": "validação pós-criação",
}
_STEP_ORDER = [
    "ban_check", "crowtado_signup", "save_partial",
    "demographics", "minute_register", "link_minute", "validate",
]
_CROWTADO_SIGNUP_RETRIES = 2
_CROWTADO_SIGNUP_RETRY_DELAY_S = 5.0


def _step_ok(detail: str = "") -> dict[str, str]:
    return {"status": "ok", "detail": detail}


def _step_skip(detail: str = "") -> dict[str, str]:
    return {"status": "skip", "detail": detail}


def _step_fail(detail: str = "") -> dict[str, str]:
    return {"status": "fail", "detail": detail}


def _hostinger_is_configured() -> bool:
    """Ao menos um perfil Hostinger com rotas (domínio catch-all)?"""
    profiles = getattr(config, "HOSTINGER_MAIL_PROFILES", None) or []
    return any(
        bool(profile.get("routes"))
        for profile in profiles
        if isinstance(profile, dict) and str(profile.get("token") or "").strip()
    )


def _registration_domains() -> list[dict[str, str]]:
    domains = []
    seen: set[str] = set()
    for profile in hostinger_mail.configured_connections():
        for route in profile["routes"]:
            # Uma rota de destinatário individual não configura um catch-all.
            if not route or "@" in route or any(c.isspace() for c in route) or route in seen:
                continue
            seen.add(route)
            domains.append({"domain": route, "profile_id": profile.get("id", ""),
                            "profile_name": profile.get("name", "")})
    return domains


def _recover_hostinger_domains() -> list[str]:
    """Completa perfis antigos sem rotas usando a própria credencial salva."""
    warnings = []
    if not any(not p["routes"] for p in hostinger_mail.configured_connections()):
        return warnings
    # Serializa com salvar/remover integrações para não restaurar dados antigos.
    with _INTEGRATION_OPERATION_LOCK:
        secure = _migrate_legacy_integrations()
        profiles = _hostinger_profiles(secure.get("hostinger") or {})
        discovered = {}
        changed = False
        recovered = []
        for profile in profiles:
            if profile["routes"]:
                recovered.append(profile)
                continue
            token = profile["token"]
            if token not in discovered:
                try:
                    discovered[token] = hostinger_mail.discover_mailboxes(token)
                except Exception as exc:
                    discovered[token] = []
                    warnings.append(_integration_error(exc, "Hostinger"))
            matches = [mailbox for mailbox in discovered[token]
                       if not profile["mailbox_id"]
                       or mailbox["resource_id"] == profile["mailbox_id"]]
            usable = [mailbox for mailbox in matches if mailbox.get("domain")]
            if usable:
                for index, mailbox in enumerate(usable):
                    recovered.append({
                        **profile,
                        "id": profile["id"] if index == 0 else uuid.uuid4().hex,
                        "mailbox_id": mailbox["resource_id"],
                        "routes": [mailbox["domain"]],
                        "name": mailbox.get("address") or profile["name"],
                    })
                changed = True
            else:
                recovered.append(profile)
                if not warnings:
                    warnings.append("A Hostinger não informou o domínio da caixa salva. "
                                    "Identifique novamente a API em Integrações.")
        if changed:
            with _PERSISTENCE_LOCK:
                secure["hostinger"] = {"profiles": recovered}
                save_secure_settings(config.INTEGRATIONS_PATH, secure)
                _apply_hostinger(recovered)
    return warnings


def _preflight_checks(domain: str = "") -> dict[str, Any]:
    """Valida todas as dependências antes de iniciar a criação de contas.

    Retorna um dict com status de cada verificação e um flag 'ready' geral.
    """
    from .. import crowtado
    checks: dict[str, dict[str, Any]] = {}

    # 1. Hostinger Mail API
    domain = domain.strip().lower().lstrip("@")
    profiles = [p for p in hostinger_mail.configured_connections()
                if domain and domain in p["routes"]]
    if profiles:
        mailboxes = 0
        working_profiles = 0
        for profile in profiles:
            try:
                result = hostinger_mail.test_connection(
                    token=profile.get("token"), mailbox=profile.get("mailbox_id"))
                mailboxes += result["mailboxes"]
                working_profiles += 1
            except Exception:
                continue
        if working_profiles:
            checks["hostinger"] = {"ok": True, "detail": f"{mailboxes} caixa(s) para {domain}"}
        else:
            checks["hostinger"] = {"ok": False, "detail": "Nenhum perfil do domínio respondeu. Confira as credenciais e a conexão."}
    else:
        checks["hostinger"] = {"ok": False, "detail": "Selecione um domínio com rota Hostinger configurada"}

    # 2. Chrome/Playwright
    try:
        from .. import crowtado
        chrome_path = crowtado._chrome_exe()
        checks["chrome"] = {"ok": True, "detail": chrome_path}
    except Exception as exc:
        checks["chrome"] = {"ok": False, "detail": str(exc)}

    # 3. Crowtado Clerk API
    try:
        import urllib.request
        from .. import tls
        req = urllib.request.Request(
            f"{crowtado.CLERK_BASE}/v1/client",
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "curl/8.5.0")
        with tls.urlopen(req, timeout=10) as resp:
            if resp.status < 300:
                checks["crowtado_api"] = {"ok": True, "detail": "Clerk API respondendo"}
            else:
                checks["crowtado_api"] = {"ok": False, "detail": f"status {resp.status}"}
    except Exception as exc:
        checks["crowtado_api"] = {"ok": False, "detail": str(exc)}

    # 4. Minute API
    try:
        import urllib.request
        from .. import tls
        req = urllib.request.Request(
            f"{config.BASE_URL}/api/v1/health",
            method="GET",
        )
        req.add_header("User-Agent", "okhttp/4.12.0")
        with tls.urlopen(req, timeout=10) as resp:
            checks["minute_api"] = {"ok": True, "detail": f"status {resp.status}"}
    except Exception as exc:
        # Minute pode não ter /health — status 404 ainda indica que a API está de pé
        error_str = str(exc)
        if "404" in error_str or "Not Found" in error_str:
            checks["minute_api"] = {"ok": True, "detail": "APIrespondendo (sem /health)"}
        else:
            checks["minute_api"] = {"ok": False, "detail": error_str}

    # 5. Invite code — tentativa rápida de join (sem commit)
    checks["invite_code"] = {
        "ok": True,
        "detail": f"código {config.INVITE_CODE} configurado",
    }

    ready = all(check.get("ok", False) for check in checks.values())
    return {"ready": ready, "checks": checks}


def _validate_minute_membership(email: str) -> None:
    profile = Session.from_email(email).ensure_auth()
    organizations = profile.get("organizations") or []
    key = org_policy.pick_org_key(email, organizations)
    if not key:
        raise RuntimeError("Organização de destino não confirmada no Minute.")
    if any(org.get("resourceKey") == key and org.get("disabled") is True
           for org in organizations if isinstance(org, dict)):
        raise RuntimeError("Conta suspensa na organização de destino.")


def _full_register_account(
    email: str, password: str, identity_data: dict[str, Any],
    *, on_step: Any = None,
) -> dict[str, Any]:
    """Fluxo completo com retry, save parcial e validação pós-criação.

    Diferente da versão anterior:
    - Crowtado signup tem retry (Turnstile pode falhar transitoriamente)
    - Credenciais são salvas LOGO após o signup Crowtado (save parcial)
    - Validação pós-criação confirma que tudo realmente funciona
    """
    import logging
    import time

    from .. import crowtado, account_bans as bans
    from ..minute_api import register as minute_register, login as minute_login

    logger = logging.getLogger("moneymin.register")
    steps: dict[str, dict[str, str]] = {}
    step_start: dict[str, float] = {}

    def _notify(step_name: str) -> None:
        step_start[step_name] = time.monotonic()
        logger.info("[register] %s — iniciando etapa: %s", email, _STEP_LABELS.get(step_name, step_name))
        if callable(on_step):
            try:
                on_step(step_name)
            except Exception:
                pass

    def _record_step(step_name: str, result: dict[str, str]) -> None:
        steps[step_name] = result
        elapsed = time.monotonic() - step_start.get(step_name, time.monotonic())
        logger.info("[register] %s — %s: %s (%.1fs)",
                     email, step_name, result["status"], elapsed)

    # Step 0: ban check
    _notify("ban_check")
    try:
        bans.require_not_banned(email)
        _record_step("ban_check", _step_ok())
    except ValueError as exc:
        _record_step("ban_check", _step_fail(str(exc)))
        return {"steps": steps, "error": f"ban: {exc}"}

    # Step 1: Crowtado signup (Chrome + Turnstile + email OTP) — com retry
    _notify("crowtado_signup")
    crowtado_signup_ok = False
    crowtado_existed = False
    last_error: str = ""
    for attempt in range(1, _CROWTADO_SIGNUP_RETRIES + 1):
        try:
            crowtado.criar_conta(email, password)
            _record_step("crowtado_signup", _step_ok(
                "conta criada" if attempt == 1 else f"conta criada (tentativa {attempt})"
            ))
            crowtado_signup_ok = True
            break
        except Exception as exc:
            error_msg = str(exc)
            lower = error_msg.lower()
            # Somente duplicidade explícita permite retomar uma conta. Erros
            # como "executable doesn't exist" não indicam cadastro existente.
            if any(marker in lower for marker in (
                "form_identifier_exists", "email_exists", "account already exists",
                "email already exists", "email address already exists",
                "email is already taken", "email address is already taken",
                "email address is taken", "email already in use",
                "email address is already in use", "conta já existe",
                "e-mail já existe", "email já existe", "e-mail já está em uso",
                "email já está em uso", "endereço de e-mail já está em uso",
            )):
                try:
                    crowtado.login(email, password)
                    _record_step("crowtado_signup", _step_skip("conta já existia; login OK"))
                    crowtado_signup_ok = True
                    crowtado_existed = True
                    break
                except Exception as login_exc:
                    _record_step("crowtado_signup", _step_fail(f"conta existe mas login falhou: {login_exc}"))
                    return {"steps": steps, "error": f"crowtado: {login_exc}"}
            last_error = error_msg
            if attempt < _CROWTADO_SIGNUP_RETRIES:
                logger.info("[register] %s — Crowtado falhou (tentativa %d/%d): %s",
                             email, attempt, _CROWTADO_SIGNUP_RETRIES, error_msg)
                time.sleep(_CROWTADO_SIGNUP_RETRY_DELAY_S)
    if not crowtado_signup_ok:
        _record_step("crowtado_signup", _step_fail(f"{last_error} (após {_CROWTADO_SIGNUP_RETRIES} tentativas)"))
        return {"steps": steps, "error": f"crowtado: {last_error}"}

    # Step 2: Salvar credenciais PARCIAIS — mesmo que as próximas etapas falhem,
    # a conta existe no Crowtado e a senha está salva para recuperação manual.
    _notify("save_partial")
    try:
        _save_crowtado_cred(email, password)
        _set_account_removed(email, False)
    except Exception:
        detail = "A conta Crowtado existe, mas não foi possível salvar o acesso local. Guarde as credenciais e confira o armazenamento antes de tentar novamente."
        _record_step("save_partial", _step_fail(detail))
        return {"steps": steps, "error": detail, "partial": True}
    _record_step("save_partial", _step_ok("credenciais salvas"))

    # Step 3: Demographics (gate obrigatório desde 26/08)
    _notify("demographics")
    try:
        if crowtado_existed:
            _record_step("demographics", _step_skip("dados da conta existente preservados"))
        else:
            crowtado.preencher_demografia(
                email, password,
                birth_month=int(identity_data["birth_month"]),
                birth_year=int(identity_data["birth_year"]),
                gender=identity_data.get("gender"),
            )
            _record_step("demographics", _step_ok("preenchida"))
    except Exception as exc:
        _record_step("demographics", _step_fail(str(exc)))
        return {"steps": steps, "error": f"demografia: {exc}"}

    # Step 4: Minute register (com invite code da org)
    _notify("minute_register")
    try:
        existed = False
        try:
            minute_register(email, password)
        except RuntimeError as exc:
            # Apenas uma resposta explícita de e-mail duplicado permite recuperação.
            detail = str(exc).casefold()
            if not any(marker in detail for marker in (
                "email_exists", "email_already_exists", "email already exists",
                "email already in use", "email-already-in-use",
            )):
                raise
            minute_login(email, password)
            existed = True
        _validate_minute_membership(email)
        _record_step("minute_register", _step_skip("já existia; acesso e organização confirmados")
                     if existed else _step_ok("registro e organização confirmados"))
    except Exception as exc:
        _record_step("minute_register", _step_fail(str(exc)))
        return {"steps": steps, "error": f"minute: {exc}"}

    # Step 5: Vincular email Minute dentro da Crowtado
    _notify("link_minute")
    try:
        try:
            crowtado.vincular_minute(email, password)
        except Exception as exc:
            # O gate explícito permite completar cadastros interrompidos sem
            # alterar demografia de contas que já estão completas.
            if not crowtado_existed or "DEMOGRAPHICS_REQUIRED" not in str(exc):
                raise
            _notify("demographics")
            try:
                crowtado.preencher_demografia(
                    email, password, birth_month=int(identity_data["birth_month"]),
                    birth_year=int(identity_data["birth_year"]), gender=identity_data.get("gender"))
                _record_step("demographics", _step_ok("cadastro incompleto recuperado"))
            except Exception as demographic_exc:
                _record_step("demographics", _step_fail(str(demographic_exc)))
                raise
            _notify("link_minute")
            crowtado.vincular_minute(email, password)
        _record_step("link_minute", _step_ok("vinculado"))
    except Exception as exc:
        _record_step("link_minute", _step_fail(str(exc)))
        return {"steps": steps, "error": f"vínculo: {exc}"}

    # Step 6: Validação pós-criação — confirma que tudo funciona
    _notify("validate")
    validation_issues: list[str] = []
    try:
        crowtado.login(email, password)
    except Exception as exc:
        validation_issues.append(f"login Crowtado: {exc}")
    try:
        minute_login(email, password)
        _validate_minute_membership(email)
    except Exception as exc:
        validation_issues.append(f"login Minute: {exc}")
    if validation_issues:
        detail = "; ".join(validation_issues)
        _record_step("validate", _step_fail(detail))
        logger.warning("[register] %s — validação parcial: %s", email, detail)
        # Não é fatal — a conta foi criada, mas algo não confere
        return {"steps": steps, "error": f"validação: {detail}", "partial": True}
    _record_step("validate", _step_ok("tudo confirmado"))
    logger.info("[register] %s — fluxo completo com sucesso", email)

    return {"steps": steps, "error": None}


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


def _removed_accounts_path() -> Path:
    """Registro local que impede a migração de ressuscitar contas removidas."""
    return config.DATA_DIR / "removed_accounts.json"


def _removed_accounts() -> set[str]:
    value = load_json(_removed_accounts_path(), {})
    emails = value.get("emails", []) if isinstance(value, dict) else []
    return {
        str(email).strip().casefold()
        for email in emails
        if str(email).strip()
    } | account_bans.banned_emails()


def _set_account_removed(email: str, removed: bool) -> None:
    normalized = email.strip().casefold()
    if not normalized:
        return
    with _PERSISTENCE_LOCK:
        emails = _removed_accounts()
        if removed:
            emails.add(normalized)
        else:
            emails.discard(normalized)
        save_json(_removed_accounts_path(), {
            "schema": 1,
            "emails": sorted(emails),
        })


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
        host = secure.get("hostinger")
        if not isinstance(host, dict) and config.HOSTINGER_MAIL_TOKEN:
            host = {
                "token": config.HOSTINGER_MAIL_TOKEN,
                "mailbox_id": config.HOSTINGER_MAILBOX_ID,
            }
            secure["hostinger"] = host
            changed = True
        if isinstance(host, dict) and not isinstance(host.get("profiles"), list):
            token = str(host.get("token") or "").strip()
            secure["hostinger"] = {
                "profiles": ([{
                    "id": uuid.uuid4().hex,
                    "name": "Caixa principal",
                    "token": token,
                    "mailbox_id": str(host.get("mailbox_id") or "").strip(),
                    "routes": [],
                }] if token else []),
            }
            host = secure["hostinger"]
            changed = True
        if isinstance(host, dict) and isinstance(host.get("profiles"), list):
            normalized_profiles = _hostinger_profiles(host)
            if normalized_profiles != host.get("profiles"):
                secure["hostinger"] = {"profiles": normalized_profiles}
                host = secure["hostinger"]
                changed = True
        if not isinstance(secure.get("ego4d"), dict):
            legacy = _legacy_aws_credentials()
            if legacy:
                secure["ego4d"] = legacy
                changed = True
        if changed:
            secure["schema"] = 2
            save_secure_settings(config.INTEGRATIONS_PATH, secure)
        if isinstance(secure.get("hostinger"), dict):
            _apply_hostinger(_hostinger_profiles(secure["hostinger"]))
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


def _hostinger_routes(value: Any) -> list[str]:
    if isinstance(value, str):
        values = value.replace(";", ",").split(",")
    elif isinstance(value, list):
        values = value
    else:
        values = []
    return sorted({
        str(item).strip().lower().lstrip("@")
        for item in values
        if str(item).strip()
    })


def _hostinger_profiles(host: dict[str, Any]) -> list[dict[str, Any]]:
    raw = host.get("profiles")
    if not isinstance(raw, list):
        raw = [host] if host.get("token") else []
    profiles: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        token = str(item.get("token") or "").strip()
        if not token:
            continue
        profiles.append({
            "id": str(item.get("id") or uuid.uuid4().hex).strip(),
            "name": str(item.get("name") or f"Caixa {index + 1}").strip(),
            "token": token,
            "mailbox_id": str(item.get("mailbox_id") or "").strip(),
            "routes": _hostinger_routes(item.get("routes")),
        })
    return profiles


def _apply_hostinger(profiles: list[dict[str, Any]]) -> None:
    config.HOSTINGER_MAIL_PROFILES = [dict(item) for item in profiles]
    primary = profiles[0] if profiles else {}
    token = str(primary.get("token") or "").strip()
    mailbox = str(primary.get("mailbox_id") or "").strip()
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
    host_profiles = _hostinger_profiles(host)
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
    host_token = str(host_profiles[0].get("token") if host_profiles else "")
    host_mailbox = str(host_profiles[0].get("mailbox_id") if host_profiles else "")
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
            "configured": bool(host_profiles),
            "connection_count": len(host_profiles),
            "token_hint": f"••••{host_token[-4:]}" if len(host_token) >= 4 else "",
            "mailbox_configured": bool(host_mailbox),
            "mailbox_hint": (
                f"••••{host_mailbox[-4:]}" if len(host_mailbox) >= 4 else ""
            ),
            "profiles": [{
                "id": item["id"],
                "name": item["name"],
                "token_hint": f"••••{item['token'][-4:]}",
                "mailbox_configured": bool(item["mailbox_id"]),
                "mailbox_hint": (
                    f"••••{item['mailbox_id'][-4:]}" if item["mailbox_id"] else ""
                ),
                "routes": item["routes"],
            } for item in host_profiles],
        },
        "holoassist": {
            "catalog_ready": holo_catalog,
            "indexes_ready": holo_indexes,
        },
        "runtime": {
            "ffmpeg_ready": readiness._binary_works(readiness.ffmpeg_bin()),
            "ffprobe_ready": readiness._binary_works(readiness.ffprobe_bin()),
            "browser_ready": readiness._private_browser_present(),
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
        "status": data.get("status"),
        "issues": [event for issue in data.get("issues", [])
                   if isinstance(issue, dict)
                   and (event := _public_event(str(issue.get("kind") or ""), issue))],
    }


# --- contas -------------------------------------------------------------------

def _list_accounts() -> list[dict[str, Any]]:
    """Contas = token_*.json em secrets/ (sem rede). org_key vem do cache de prefs."""
    prefs = _load_prefs()
    health = load_json(ACCOUNT_HEALTH_PATH, {})
    if not isinstance(health, dict):
        health = {}
    org_keys = prefs.get("org_keys", {})
    removed = _removed_accounts()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted(config.tokens_dir().glob("token_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        email = data.get("email")
        if not email:
            continue
        if str(email).strip().casefold() in removed:
            continue
        key = str(email).strip().casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "email": email,
            "expires_at": data.get("expires_at", 0),
            "org_key": org_keys.get(email),
            "account_kind": org_policy.account_kind(email),
            "last_check": health.get(key, {}),
            "org_name": {
                config.ORG_KEY: "Datoric", config.CLARU_ORG_KEY: "Claru",
                config.HUB_ORG_KEY: "Hub antigo",
            }.get(org_keys.get(email), "Não verificada"),
        })
    return out


def _resolve_org(email: str, session: Session | None = None) -> str:
    """Resolve (e cacheia) a org_key pela política da conta, não pela 1ª org."""
    # Validar sempre, inclusive quando a organização já está no cache. O HUB
    # devolve /users/me = 200 para contas desativadas; ensure_auth inspeciona o
    # campo `disabled` e impede que a campanha comece com uma conta bloqueada.
    sess = session or Session.from_email(email)
    profile = sess.ensure_auth()
    orgs = [org for org in (profile.get("organizations") or []) if isinstance(org, dict)]
    org_key = org_policy.ensure_membership(sess, email, orgs)
    with _PERSISTENCE_LOCK:
        prefs = _load_prefs()
        prefs.setdefault("org_keys", {})[email] = org_key
        _save_prefs(prefs)
    return org_key


def _check_account_health(email: str) -> dict[str, Any]:
    """Verificação sem migração: falhas temporárias não condenam uma conta."""
    session = None
    for attempt in range(1, 3):
        try:
            session = session or Session.from_email(email)
            profile = session.ensure_auth()
            org_key = org_policy.pick_org_key(email, profile.get("organizations") or [])
            if not org_key:
                raise AuthError("Organização de destino não confirmada.", code="organization")
            if any(isinstance(org, dict) and org.get("resourceKey") == org_key
                   and org.get("disabled") is True for org in profile.get("organizations", [])):
                raise AuthError("Restrição explícita na organização de destino.", code="restricted")
            result = {
                "email": email, "status": "active", "status_label": "Acesso verificado",
                "org_key": org_key, "expires_at": session.data.get("expires_at", 0),
            }
            break
        except Exception as exc:  # noqa: BLE001 — qualquer falha ambígua é inconclusiva
            issue = account_issue(email, exc)
            if issue["retryable"] and attempt < 2:
                time.sleep(2.0 if issue["code"] == "rate_limit" else 0.5)
                continue
            status, label = {
                "restricted": ("disabled", "Restrição confirmada"),
                "authentication": ("needs_reauth", "Reconectar acesso"),
                "missing_access": ("needs_reauth", "Reconectar acesso"),
                "organization": ("needs_org", "Organização pendente"),
            }.get(issue["code"], ("inconclusive", "Verificação inconclusiva"))
            result = {"email": email, "status": status, "status_label": label,
                      "error": issue_text(issue), "issue": issue}
            break
    result["attempts"] = attempt
    result["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    _save_account_check(email, result)
    return result


def _save_account_check(email: str, result: dict[str, Any]) -> None:
    with _PERSISTENCE_LOCK:
        health = load_json(ACCOUNT_HEALTH_PATH, {})
        if not isinstance(health, dict):
            health = {}
        previous = health.get(email.strip().casefold(), {})
        result["last_success_at"] = (result["checked_at"] if result["status"] == "active"
                                     else previous.get("last_success_at") if isinstance(previous, dict) else None)
        health[email.strip().casefold()] = dict(result)
        try:
            save_json(ACCOUNT_HEALTH_PATH, health)
            if result["status"] == "disabled" and result.get("issue", {}).get("restriction_confirmed"):
                _ban_accounts([result["issue"]])
                result["permanently_removed"] = True
            if result["status"] == "active" and result.get("org_key"):
                prefs = _load_prefs()
                prefs.setdefault("org_keys", {})[email] = result["org_key"]
                _save_prefs(prefs)
        except (OSError, ValueError):
            # Falha no histórico local não altera o diagnóstico remoto.
            result["history_saved"] = False


def _migrate_account_org(email: str) -> dict[str, Any]:
    kind = org_policy.account_kind(email)
    row = {"email": email, "account_kind": kind}
    if kind == "claru":
        return {**row, "status": "skipped", "message": "Claru: mantida sem aplicar código."}
    session = Session.from_email(email)
    profile = session.ensure_auth()
    before = org_policy.pick_org_key(email, profile.get("organizations") or [])
    target = org_policy.ensure_membership(session, email, profile.get("organizations") or [])
    with _PERSISTENCE_LOCK:
        prefs = _load_prefs()
        prefs.setdefault("org_keys", {})[email] = target
        _save_prefs(prefs)
    return {
        **row, "status": "already" if before else "migrated", "org_key": target,
        "message": ("Já estava na organização nova." if before else "Organização atualizada.")
                   + f" Crowtado · {config.INVITE_CODE}",
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
    """Reutiliza credenciais da mesma identidade, inclusive lotes de criação."""
    creds: dict[str, str] = {}
    def collect(rec):
        if not isinstance(rec, dict):
            return
        email = rec.get("email")
        password = rec.get("password") or rec.get("senha")
        if isinstance(email, str) and isinstance(password, str) and password:
            creds[email.strip().casefold()] = password

    for path in sorted(config.DATA_DIR.glob("novas_contas_*.json")):
        rows = load_json(path, [])
        if isinstance(rows, list):
            for rec in rows:
                collect(rec)
    contas = config.DATA_DIR / "contas.jsonl"
    try:
        for line in contas.read_bytes().splitlines():
            try:
                rec = json.loads(line.decode("utf-8-sig"))
            except (UnicodeError, json.JSONDecodeError):
                continue
            collect(rec)
    except OSError:
        pass
    stored = load_json(CROWTADO_PW_PATH, {})
    if isinstance(stored, dict):
        creds.update({str(email).strip().casefold(): password
                      for email, password in stored.items()
                      if isinstance(password, str) and password})
    return creds


def _configured_crowtado_creds() -> dict[str, str]:
    """Mantém a grafia da conta ativa e não recupera identidades removidas."""
    saved = {str(e).strip().casefold(): p for e, p in _crowtado_creds().items()}
    return {a["email"]: saved[str(a["email"]).strip().casefold()]
            for a in _list_accounts() if str(a["email"]).strip().casefold() in saved}


def _save_crowtado_cred(email: str, password: str) -> None:
    with _PERSISTENCE_LOCK:
        stored = load_json(CROWTADO_PW_PATH, {})
        creds = stored if isinstance(stored, dict) else {}
        normalized = email.strip().casefold()
        creds = {key: value for key, value in creds.items()
                 if str(key).strip().casefold() != normalized}
        creds[normalized] = password
        save_json(CROWTADO_PW_PATH, creds)


def _remove_account_data(email: str) -> None:
    """Remove caches editáveis ligados à conta (o cadastro histórico fica intacto)."""
    with _PERSISTENCE_LOCK:
        stored = load_json(CROWTADO_PW_PATH, {})
        creds = stored if isinstance(stored, dict) else {}
        matching_creds = [
            key for key in creds
            if str(key).casefold() == email.casefold()
        ]
        for key in matching_creds:
            creds.pop(key, None)
        if matching_creds:
            save_json(CROWTADO_PW_PATH, creds)

        balances = _load_balances()
        matching_balances = [
            key for key in balances
            if str(key).casefold() == email.casefold()
        ]
        for key in matching_balances:
            balances.pop(key, None)
        if matching_balances:
            _save_balances(balances)
    crowtado.clear_cached_session(email)


def _ban_accounts(issues: list[dict]) -> None:
    """Registra primeiro a restrição, depois remove o acesso local permanentemente."""
    if not issues or any(i.get("restriction_confirmed") is not True for i in issues):
        raise ValueError("A remoção exige restrição confirmada pela plataforma.")
    with _PERSISTENCE_LOCK:
        path = config.DATA_DIR / "banned_accounts.json"
        archive = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {"schema": 1, "accounts": []}
        if (not isinstance(archive, dict) or not isinstance(archive.get("accounts"), list)
                or any(not isinstance(row, dict) or not isinstance(row.get("email"), str)
                       for row in archive.get("accounts", []))):
            raise ValueError("Registro de contas banidas inválido; remoção cancelada.")
        records = {row["email"].casefold(): row for row in archive["accounts"]}
        passwords = _crowtado_creds()
        for issue in issues:
            email = account_transfer.email_key(issue.get("email"))
            previous = records.get(email, {})
            banned_at = previous.get("banned_at") or previous.get("removed_at") or time.strftime("%Y-%m-%dT%H:%M:%S%z")
            records[email] = {"email": email, "password": passwords.get(email) or previous.get("password"),
                              "banned_at": banned_at, "removed_at": previous.get("removed_at") or banned_at,
                              "reason": issue.get("reason", "Restrição confirmada pela plataforma."),
                              "stage": issue.get("stage", "Envio"), "restriction_confirmed": True}
        archive["accounts"] = list(records.values())
        save_json(path, archive)
        for issue in issues:
            email = account_transfer.email_key(issue["email"])
            _set_account_removed(email, True)
            config.token_path(email).unlink(missing_ok=True)
            prefs = _load_prefs()
            prefs["selected_accounts"] = [e for e in prefs.get("selected_accounts", []) if e.casefold() != email]
            prefs["org_keys"] = {e: key for e, key in prefs.get("org_keys", {}).items() if e.casefold() != email}
            _save_prefs(prefs)
            _remove_account_data(email)
        account_bans.purge_local_records({account_transfer.email_key(i["email"]) for i in issues})


def _preflight_fingerprint(emails: list[str]) -> str:
    digest = hashlib.sha256()
    for path in [*[config.token_path(e) for e in sorted(set(emails))], _removed_accounts_path()]:
        digest.update(str(path).encode())
        digest.update(path.read_bytes() if path.exists() else b"missing")
    return digest.hexdigest()


def _on_balance_result(email: str, summary: dict | None, erro: str | None) -> None:
    with _PERSISTENCE_LOCK:
        balances = _load_balances()
        rec = dict(balances.get(email) or {})
        rec["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        if summary is not None:
            rec.update(summary)
            rec["updated_at"] = rec["checked_at"]
            rec["error"] = None
            rec["issue"] = None
            rec["stale"] = False
        else:
            issue = account_issue(email, RuntimeError(erro or "Consulta inconclusiva"), stage="Consulta de saldo Crowtado")
            rec["error"] = issue_text(issue)
            rec["issue"] = issue
            rec["stale"] = True
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
    """Valida o intervalo de duração solicitado pela interface."""
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
    preflights: dict[str, dict] = {}
    banned_monitor = BannedMonitor()
    RUNNER.on_restriction = lambda email: _ban_accounts([{
        "email": email, "restriction_confirmed": True, "stage": "Envio da campanha",
        "reason": "Restrição confirmada pela plataforma durante o envio.",
    }])
    # No QMoney o Flask e apenas o servico local consumido pela interface Qt.
    # Nenhum frontend web e publicado ou usado como fallback.
    app = Flask(__name__, static_folder=None)

    @app.before_request
    def validate_json_body():
        if (request.path.startswith("/api/") and request.method in ("POST", "PUT", "PATCH")
                and request.get_data(cache=True)):
            if not request.is_json or not isinstance(request.get_json(silent=True), dict):
                return jsonify({"error": "O corpo da requisição deve ser um objeto JSON válido."}), 400

    @app.before_request
    def guard_account_operations():
        if request.path.startswith("/api/integrations/") and request.method in ("PUT", "DELETE"):
            _INTEGRATION_OPERATION_LOCK.acquire()
            g.integration_operation_locked = True
        account_write = request.path.startswith("/api/accounts") and request.method != "GET"
        campaign_start = request.path in ("/api/campaigns", "/api/campaigns/preflight") and request.method == "POST"
        if account_write or campaign_start or request.path == "/api/tasks":
            _ACCOUNT_OPERATION_LOCK.acquire()
            g.account_operation_locked = True
            with _BULK_REGISTER_LOCK:
                if _BULK_REGISTER_STATE.get("state") == "running":
                    return jsonify({"error": "Aguarde o cadastro em lote terminar."}), 409
            if ORG_MIGRATION.running:
                return jsonify({"error": "Aguarde a migração das organizações terminar."}), 409

    @app.teardown_request
    def release_account_operation(_error):
        if g.pop("integration_operation_locked", False):
            _INTEGRATION_OPERATION_LOCK.release()
        if g.pop("account_operation_locked", False):
            _ACCOUNT_OPERATION_LOCK.release()

    @app.get("/")
    def index():
        return jsonify({"service": "qmoney", "ui": "qt", "ok": True})

    # -- contas ---------------------------------------------------------------
    @app.get("/api/accounts")
    def get_accounts():
        return jsonify({"accounts": _list_accounts()})

    @app.get("/api/accounts/migration")
    def get_org_migration():
        response = jsonify(ORG_MIGRATION.snapshot())
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/accounts/migration")
    def start_org_migration():
        if RUNNER.running or BALANCES_RUNNER.running:
            return jsonify({"error": "Aguarde a campanha ou consulta de saldos terminar antes de migrar."}), 409
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "Seleção de contas inválida."}), 400
        known = {item["email"].strip().casefold(): item["email"] for item in _list_accounts()}
        selected = body.get("emails", list(known))
        if not isinstance(selected, list) or not selected or any(
                not isinstance(email, str) or email.strip().casefold() not in known for email in selected):
            return jsonify({"error": "Selecione contas cadastradas no QMoney."}), 400
        emails = list(dict.fromkeys(known[email.strip().casefold()] for email in selected))
        try:
            result = ORG_MIGRATION.start(emails, _migrate_account_org)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        except OSError:
            return jsonify({"error": "Não foi possível salvar o relatório da migração."}), 500
        return jsonify(result), 202

    @app.get("/api/accounts/banned/monitor")
    def banned_monitor_snapshot():
        archive = load_json(config.DATA_DIR / "banned_accounts.json", {"accounts": []})
        rows = [{"email": row["email"], "banned_at": row.get("banned_at") or row.get("removed_at"),
                 "has_password": bool(row.get("password")), "monitor": row.get("monitor", {})}
                for row in archive.get("accounts", [])]
        return jsonify({"accounts": rows, "runner": banned_monitor.snapshot()})

    @app.post("/api/accounts/banned/refresh")
    def refresh_banned_monitor():
        with _PERSISTENCE_LOCK:
            archive = load_json(config.DATA_DIR / "banned_accounts.json", {"accounts": []})
            rows = archive.get("accounts", [])
            if not rows:
                return jsonify({"error": "Não há contas banidas registradas."}), 400
            def save(email, result):
                with _PERSISTENCE_LOCK:
                    path = config.DATA_DIR / "banned_accounts.json"
                    current = load_json(path, {"accounts": []})
                    for row in current.get("accounts", []):
                        if row.get("email", "").casefold() == email.casefold():
                            row["monitor"] = result
                    save_json(path, current)
            try:
                banned_monitor.start(rows, save)
            except RuntimeError as exc:
                return jsonify({"error": str(exc)}), 409
        return jsonify(banned_monitor.snapshot()), 202

    @app.get("/api/accounts/banned")
    def banned_accounts():
        with _PERSISTENCE_LOCK:
            path = config.DATA_DIR / "banned_accounts.json"
            archive = load_json(path, {"schema": 1, "accounts": []})
            passwords = _crowtado_creds()
            changed = False
            for row in archive.get("accounts", []):
                email = str(row.get("email", "")).strip().casefold()
                for key, value in (("password", row.get("password") or passwords.get(email)),
                                   ("banned_at", row.get("banned_at") or row.get("removed_at"))):
                    if key not in row or row[key] != value:
                        row[key] = value
                        changed = True
            if changed:
                save_json(path, archive)
            return jsonify(archive)

    @app.post("/api/accounts/import")
    def import_accounts():
        if request.content_length and request.content_length > account_transfer.MAX_BYTES * 2:
            return jsonify({"error": "Arquivo muito grande; limite de 10 MB."}), 413
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("content"), str) or type(body.get("apply", False)) is not bool:
            return jsonify({"error": "Informe o conteúdo JSON e uma opção de importação válida."}), 400
        if body.get("apply") and (RUNNER.running or BALANCES_RUNNER.running):
            return jsonify({"error": "Aguarde a campanha ou consulta de saldos terminar antes de importar."}), 409
        try:
            with _PERSISTENCE_LOCK:
                result = account_transfer.import_accounts(body["content"], apply=body.get("apply", False),
                    passwords_path=CROWTADO_PW_PATH, removed_path=_removed_accounts_path())
            return jsonify(result)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except OSError:
            return jsonify({"error": "Não foi possível acessar os arquivos locais das contas."}), 500

    @app.post("/api/accounts/export")
    def export_accounts():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or ("emails" in body and not isinstance(body["emails"], list)):
            return jsonify({"error": "Seleção de contas inválida."}), 400
        try:
            with _PERSISTENCE_LOCK:
                result = account_transfer.export_accounts(body.get("emails"), _crowtado_creds(), _removed_accounts())
            response = jsonify(result)
            response.headers["Cache-Control"] = "no-store"
            return response
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except OSError:
            return jsonify({"error": "Não foi possível ler as contas para exportação."}), 500

    @app.post("/api/accounts")
    def add_account():
        body = request.get_json(silent=True) or {}
        email = str(body.get("email", "")).strip()
        password = str(body.get("password", ""))
        if not email or not password:
            return jsonify({"error": "informe email e senha"}), 400
        try:
            account_bans.require_not_banned(email)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        try:
            login(email, password)
        except (RuntimeError, OSError) as exc:
            return jsonify({"error": str(exc)}), 400
        _set_account_removed(email, False)
        # A tela pede a senha da identidade Minute / Crowtado. Antes este
        # caminho salvava somente o token Minute, então a mesma conta aparecia
        # em Saldos como se não fosse uma conta Crowtado.
        _save_crowtado_cred(email, password)
        return jsonify({"ok": True, "email": email})

    @app.post("/api/accounts/register")
    def register_account():
        """Fluxo completo de 6 etapas para uma conta.

        Body: {email, password, gender?}. Se gender não vier, gera dados
        demográficos aleatórios. Roda Crowtado signup → demografia → Minute
        → vínculo → salva. Retorna o dict de steps.
        """
        body = request.get_json(silent=True) or {}
        email = str(body.get("email", "")).strip()
        password = str(body.get("password", ""))
        if not email or not password:
            return jsonify({"error": "informe email e senha"}), 400
        if not _hostinger_is_configured():
            return jsonify({"error": "Configure a integração Hostinger (domínio catch-all) antes de criar contas."}), 409
        gender = str(body.get("gender", "")).strip() or None
        if not gender:
            gender = "male" if random.random() < 0.48 else "female"
        identity_data: dict[str, Any] = {
            "nome": email.split("@")[0],
            "sobrenome": "",
            "email": email,
            "senha": password,
            "gender": gender,
            "birth_month": random.randint(1, 12),
            "birth_year": random.randint(1980, 2003),
        }
        result = _full_register_account(email, password, identity_data)
        ok = result["error"] is None
        return jsonify({"ok": ok, "email": email, "steps": result["steps"],
                         "error": result["error"]}), 200 if ok else 400

    @app.get("/api/accounts/domains")
    def list_account_domains():
        """Domínios disponíveis para o criador de contas (perfis Hostinger)."""
        warnings = _recover_hostinger_domains()
        domains = _registration_domains()
        return jsonify({
            "domains": domains,
            "webmail_url": "https://webmail.hostinger.com" if domains else "",
            "hostinger_configured": _hostinger_is_configured(),
            "warning": " ".join(warnings),
        })

    @app.get("/api/accounts/bulk-register/preflight")
    def bulk_register_preflight():
        """Valida todas as dependências antes de iniciar a criação em lote."""
        result = _preflight_checks(request.args.get("domain", ""))
        return jsonify(result)

    @app.get("/api/accounts/bulk-register/status")
    def bulk_register_status():
        with _BULK_REGISTER_LOCK:
            return jsonify(dict(_BULK_REGISTER_STATE))

    @app.post("/api/accounts/bulk-register")
    def bulk_register_accounts():
        """Cria N contas novas com o fluxo completo (Crowtado + Minute + vínculo).

        Body: {count, domain}. Gera identidades via identity.gerar_identidade(),
        executa as 6 etapas por conta e reporta progresso via /status.
        """
        with _BULK_REGISTER_LOCK:
            if _BULK_REGISTER_STATE.get("state") == "running":
                return jsonify({"error": "Já existe uma criação em andamento."}), 409

        body = request.get_json(silent=True) or {}
        try:
            count = int(body.get("count", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "count inválido"}), 400
        domain = str(body.get("domain", "")).strip().lower().lstrip("@")
        if count < 1 or count > 50:
            return jsonify({"error": "count deve estar entre 1 e 50"}), 400
        if not domain:
            return jsonify({"error": "domain é obrigatório"}), 400
        if RUNNER.running:
            return jsonify({"error": "pare a campanha antes de criar contas"}), 409
        if not _hostinger_is_configured():
            return jsonify({"error": "Configure a integração Hostinger (domínio catch-all) antes de criar contas."}), 409

        if domain not in {row["domain"] for row in _registration_domains()}:
            return jsonify({"error": "Selecione um domínio catch-all configurado nas integrações."}), 400

        existing_emails = {
            account["email"].lower()
            for account in _list_accounts()
        }

        with _BULK_REGISTER_LOCK:
            _BULK_REGISTER_STATE.clear()
            _BULK_REGISTER_STATE.update({
                "state": "running",
                "total": count,
                "completed": 0,
                "created": 0,
                "failed": 0,
                "results": [],
                "current_email": "",
                "current_step": "",
            })

        def _run_batch() -> None:
            successes = 0
            failures = 0
            results: list[dict[str, Any]] = []
            used_emails: set[str] = set(existing_emails)
            for index in range(count):
                try:
                    identity_data = identity.gerar_identidade(
                        domain=domain,
                        existentes=used_emails,
                    )
                except RuntimeError as exc:
                    with _BULK_REGISTER_LOCK:
                        _BULK_REGISTER_STATE["state"] = "failed"
                        _BULK_REGISTER_STATE["error"] = str(exc)
                    return
                email = identity_data["email"]
                password = identity_data["senha"]
                used_emails.add(email.lower())

                def _on_step(step_name: str) -> None:
                    with _BULK_REGISTER_LOCK:
                        _BULK_REGISTER_STATE["current_email"] = email
                        _BULK_REGISTER_STATE["current_step"] = _STEP_LABELS.get(step_name, step_name)

                result = _full_register_account(email, password, identity_data, on_step=_on_step)
                ok = result["error"] is None
                if ok:
                    successes += 1
                else:
                    failures += 1
                results.append({
                    "email": email,
                    "nome": identity_data["nome"],
                    "sobrenome": identity_data["sobrenome"],
                    "gender": identity_data["gender"],
                    "birth_month": identity_data["birth_month"],
                    "birth_year": identity_data["birth_year"],
                    "created": ok,
                    "error": result["error"],
                    "steps": result["steps"],
                })
                with _BULK_REGISTER_LOCK:
                    _BULK_REGISTER_STATE["completed"] = index + 1
                    _BULK_REGISTER_STATE["created"] = successes
                    _BULK_REGISTER_STATE["failed"] = failures
                    _BULK_REGISTER_STATE["results"] = list(results)
        def _worker() -> None:
            terminal = {"state": "done"}
            try:
                _run_batch()
            except Exception:
                terminal = {
                    "state": "failed",
                    "error": "Não foi possível concluir o cadastro. Confira as contas e credenciais salvas antes de tentar novamente.",
                }
            finally:
                with _BULK_REGISTER_LOCK:
                    _BULK_REGISTER_STATE.update(terminal)
                    _BULK_REGISTER_STATE["current_email"] = ""
                    _BULK_REGISTER_STATE["current_step"] = ""

        try:
            thread = threading.Thread(target=_worker, name="moneymin-bulk-register", daemon=True)
            thread.start()
        except Exception:
            with _BULK_REGISTER_LOCK:
                _BULK_REGISTER_STATE.update(state="failed", error="Não foi possível iniciar o cadastro.")
            return jsonify({"error": "Não foi possível iniciar o cadastro."}), 500
        return jsonify({"ok": True, "total": count, "domain": domain})

    @app.delete("/api/accounts/<email>")
    def remove_account(email: str):
        if RUNNER.running:
            return jsonify({"error": "pare a campanha antes de remover uma conta"}), 409
        if BALANCES_RUNNER.running:
            return jsonify({"error": "aguarde a consulta de saldos terminar"}), 409
        path = config.token_path(email)
        if not path.exists():
            return jsonify({"error": f"conta não encontrada: {email}"}), 404
        # Grave primeiro a intenção. Mesmo se o Windows interromper a remoção
        # física, a conta já não reaparece nem volta para uma campanha.
        _set_account_removed(email, True)
        try:
            path.unlink(missing_ok=True)
            with _PERSISTENCE_LOCK:
                prefs = _load_prefs()
                normalized = email.casefold()
                org_keys = prefs.get("org_keys", {})
                if isinstance(org_keys, dict):
                    for key in list(org_keys):
                        if str(key).casefold() == normalized:
                            org_keys.pop(key, None)
                selected = prefs.get("selected_accounts")
                if isinstance(selected, list):
                    prefs["selected_accounts"] = [
                        value for value in selected
                        if str(value).casefold() != normalized
                    ]
                _save_prefs(prefs)
            _remove_account_data(email)
        except OSError as exc:
            return jsonify({
                "ok": True,
                "warning": f"a conta não voltará, mas alguns dados locais aguardam nova tentativa de limpeza: {exc}",
            })
        return jsonify({"ok": True})

    @app.post("/api/accounts/<email>/check")
    def check_account(email: str):
        result = _check_account_health(email)
        ok = result["status"] == "active"
        return jsonify({"ok": ok, **result}), 200 if ok else 400

    @app.post("/api/accounts/check-all")
    def check_all_accounts():
        if RUNNER.running:
            return jsonify({
                "error": "aguarde ou pare a campanha antes de verificar todas as contas",
            }), 409
        emails = [account["email"] for account in _list_accounts()]
        results_by_email: dict[str, dict[str, Any]] = {}
        if emails:
            workers = min(3, len(emails))
            with ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix="moneymin-account-check") as pool:
                futures = {pool.submit(_check_account_health, email): email
                           for email in emails}
                for future in as_completed(futures):
                    email = futures[future]
                    try:
                        results_by_email[email] = future.result()
                    except Exception as exc:  # noqa: BLE001 — uma conta não mata o lote
                        issue = account_issue(email, exc)
                        results_by_email[email] = {
                            "email": email, "status": "inconclusive", "status_label": "Verificação inconclusiva",
                            "error": issue_text(issue), "issue": issue,
                        }
        results = [results_by_email[email] for email in emails]
        return jsonify({
            "ok": True,
            "total": len(results),
            "active": sum(result["status"] == "active" for result in results),
            "disabled": [result for result in results
                         if result["status"] == "disabled"],
            "errors": [result for result in results if result["status"] not in ("active", "disabled")],
            "inconclusive": sum(result["status"] == "inconclusive" for result in results),
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
        host = (secure.get("hostinger")
                if isinstance(secure.get("hostinger"), dict) else {})
        profiles = _hostinger_profiles(host)
        if body.get("auto_detect"):
            token = str(body.get("token") or "").strip()
            if len(token) < 16:
                return jsonify({
                    "error": "cole um token válido da API Mail da Hostinger",
                }), 400
            duplicates = [
                item["name"] for item in profiles
                if item["token"] == token and item.get("mailbox_id")
            ]
            if duplicates and all(item["routes"] for item in profiles if item["token"] == token):
                return jsonify({
                    "error": (
                        "esta API Hostinger já está conectada"
                        + (f" ({', '.join(duplicates)})" if duplicates else "")
                    ),
                    "code": "hostinger_already_connected",
                }), 409
            try:
                detected = hostinger_mail.discover_mailboxes(token)
            except Exception as exc:  # noqa: BLE001
                return jsonify({"error": _integration_error(exc, "Hostinger")}), 400
            detected_ids = {item["resource_id"] for item in detected}
            existing_by_mailbox = {
                item["mailbox_id"]: item
                for item in profiles if item.get("mailbox_id")
            }
            # Uma conexão antiga sem ID de caixa é substituída quando o mesmo
            # token é identificado. As demais APIs permanecem intactas.
            profiles = [
                item for item in profiles
                if item.get("mailbox_id") not in detected_ids
                and not (item["token"] == token and not item.get("mailbox_id"))
            ]
            imported = []
            for index, mailbox in enumerate(detected):
                previous = existing_by_mailbox.get(mailbox["resource_id"], {})
                address = mailbox.get("address") or ""
                domain = mailbox.get("domain") or ""
                item = {
                    "id": previous.get("id") or uuid.uuid4().hex,
                    "name": address or f"Caixa Hostinger {index + 1}",
                    "token": token,
                    "mailbox_id": mailbox["resource_id"],
                    "routes": [domain] if domain else [],
                }
                profiles.append(item)
                imported.append({
                    "name": item["name"],
                    "domain": domain,
                })
            with _PERSISTENCE_LOCK:
                secure["schema"] = 2
                secure["hostinger"] = {"profiles": profiles}
                save_secure_settings(config.INTEGRATIONS_PATH, secure)
                _apply_hostinger(profiles)
            return jsonify({
                "ok": True,
                "detected_count": len(imported),
                "detected": imported,
                "integrations": _integration_snapshot(),
            })
        profile_id = str(body.get("profile_id") or "").strip()
        current = next(
            (item for item in profiles if item["id"] == profile_id), {})
        # Compatibilidade com a tela antiga: quando havia apenas uma conexão,
        # um PUT sem id significava editar aquela conexão, não criar outra.
        if not current and not body.get("create") and len(profiles) == 1:
            current = profiles[0]
            profile_id = current["id"]
        values = {
            "id": profile_id or uuid.uuid4().hex,
            "name": str(body.get("name") or current.get("name")
                        or f"Caixa {len(profiles) + 1}").strip(),
            "token": str(body.get("token") or current.get("token") or "").strip(),
            "mailbox_id": str(
                body.get("mailbox_id") if "mailbox_id" in body
                else current.get("mailbox_id") or "").strip(),
            "routes": _hostinger_routes(
                body.get("routes") if "routes" in body
                else current.get("routes") or []),
        }
        if len(values["token"]) < 16:
            return jsonify({"error": "informe um token válido da API Mail da Hostinger"}), 400
        if any(item["token"] == values["token"] and item["id"] != values["id"]
               for item in profiles):
            return jsonify({
                "error": "esta API Hostinger já está conectada",
                "code": "hostinger_already_connected",
            }), 409
        try:
            tested = hostinger_mail.test_connection(
                token=values["token"], mailbox=values["mailbox_id"] or None)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": _integration_error(exc, "Hostinger")}), 400
        replaced = False
        for index, item in enumerate(profiles):
            if item["id"] == values["id"]:
                profiles[index] = values
                replaced = True
                break
        if not replaced:
            profiles.append(values)
        with _PERSISTENCE_LOCK:
            secure["schema"] = 2
            secure["hostinger"] = {"profiles": profiles}
            save_secure_settings(config.INTEGRATIONS_PATH, secure)
            _apply_hostinger(profiles)
        return jsonify({"ok": True, "profile_id": values["id"], "test": tested,
                        "integrations": _integration_snapshot()})

    @app.delete("/api/integrations/hostinger/<profile_id>")
    def delete_hostinger_integration(profile_id: str):
        secure = _migrate_legacy_integrations()
        host = (secure.get("hostinger")
                if isinstance(secure.get("hostinger"), dict) else {})
        profiles = _hostinger_profiles(host)
        remaining = [item for item in profiles if item["id"] != profile_id]
        if len(remaining) == len(profiles):
            return jsonify({"error": "conexão Hostinger não encontrada"}), 404
        with _PERSISTENCE_LOCK:
            secure["schema"] = 2
            secure["hostinger"] = {"profiles": remaining}
            save_secure_settings(config.INTEGRATIONS_PATH, secure)
            _apply_hostinger(remaining)
        return jsonify({"ok": True, "integrations": _integration_snapshot()})

    @app.post("/api/integrations/hostinger/test")
    def test_hostinger_integration():
        secure = _migrate_legacy_integrations()
        host = (secure.get("hostinger")
                if isinstance(secure.get("hostinger"), dict) else {})
        profiles = _hostinger_profiles(host)
        body = request.get_json(silent=True) or {}
        profile_id = str(body.get("profile_id") or "").strip()
        current = next(
            (item for item in profiles if item["id"] == profile_id), {})
        if not current and not body.get("create") and len(profiles) == 1:
            current = profiles[0]
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

    @app.get("/api/health")
    def get_health():
        """Handshake leve: nunca abrir ferramentas, ler contas ou percorrer mídia."""
        return jsonify({
            "ok": True,
            "service": {
                "app_version": os.environ.get("QMONEY_APP_VERSION", "unknown"),
            },
        })

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
        accounts: list[AccountSpec] = []
        account_errors: list[str] = []
        account_issues: list[dict[str, Any]] = []
        for email in missing_accounts:
            account_issues.append(account_issue(email, AuthError("sem token salvo")))
        if emails:
            resolved: list[AccountSpec | None] = [None] * len(emails)
            with ThreadPoolExecutor(max_workers=max(1, min(6, len(emails)))) as pool:
                futures = {pool.submit(_resolve_org, email): i
                           for i, email in enumerate(emails) if email in known}
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        resolved[index] = AccountSpec(emails[index], future.result())
                    except (AuthError, RuntimeError, OSError) as exc:
                        account_issues.append(account_issue(emails[index], exc))
            accounts = [account for account in resolved if account is not None]

        selected: list[dict[str, Any]] = []
        unavailable: list[str] = []
        catalog_loaded = False
        if accounts and raw_tasks:
            try:
                catalog = campaign.available_tasks(
                    accounts[0].email, accounts[0].org_key,
                    min_dur_s=min_dur_s, max_dur_s=max_dur_s,
                    include_unavailable=True, dataset_provider=provider)
                catalog_loaded = True
            except (AuthError, RuntimeError, OSError, json.JSONDecodeError) as exc:
                account_issues.append(account_issue(
                    accounts[0].email, exc, stage="Carregamento das categorias"))
                catalog = []
            by_id = {str(item.get("id")): item for item in catalog if item.get("id")}
            seen: set[str] = set()
            for raw in raw_tasks if catalog_loaded else []:
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
                    account_issues.append(account_issue(
                        account.email, exc, stage="Consulta das categorias"))
                    continue
                missing = selected_ids - account_ids
                if missing:
                    blockers.append(
                        f"{account.email} não possui {len(missing)} categoria(s) selecionada(s)"
                    )

        if unavailable:
            warnings.append(f"{len(unavailable)} categoria(s) sem clipe na duração escolhida")
        if raw_tasks and catalog_loaded and not selected:
            blockers.append("nenhuma categoria selecionada possui clipe compatível")

        account_issues.sort(key=lambda item: (emails.index(item["email"]), item["stage"]))
        account_errors = [issue_text(item) for item in account_issues]
        # Compatibilidade: clientes antigos leem apenas blockers. Não esconder
        # a conta nem classificar falhas de rede como credenciais inválidas.
        blockers.extend(account_errors)

        clip_count = sum(int(item.get("clip_count") or 0) for item in selected)
        if target_hours > 0:
            estimated_sends = max(1, math.ceil(target_hours * 4)) * len(accounts)
        else:
            estimated_sends = len(selected) * count * len(accounts)
        try:
            ready = readiness.campaign_readiness(provider)
        except Exception as exc:  # noqa: BLE001 — ainda devolve os outros checks
            ready = {"ready": False, "checks": [], "error": str(exc)}
        readiness_errors = [
            item for item in ready.get("checks", [])
            if item.get("status") == "error"
        ]
        for item in readiness_errors:
            message = f"{item.get('name')}: {item.get('detail')}"
            if message not in blockers:
                blockers.append(message)
        if not ready.get("ready") and not readiness_errors and ready.get("error"):
            blockers.append(f"prontidão indisponível: {ready['error']}")
        storage = _storage_snapshot(include_path=False)
        if storage.get("free_bytes", 0) < 10 * 1024 ** 3:
            warnings.append("há menos de 10 GiB livres na unidade da biblioteca")

        removable = {i["email"] for i in account_issues if i.get("restriction_confirmed") is True}
        survivors = [a for a in accounts if a.email not in removable]
        reusable = (bool(survivors) and bool(selected) and catalog_loaded and ready.get("ready") is True
                    and len(blockers) == len(account_errors)
                    and all(i.get("restriction_confirmed") is True for i in account_issues))
        receipt_id = None
        if reusable:
            now = time.monotonic()
            for key in list(preflights):
                if preflights[key]["expires"] <= now:
                    preflights.pop(key)
            if len(preflights) >= 32:
                preflights.pop(next(iter(preflights)))
            receipt_id = uuid.uuid4().hex
            preflights[receipt_id] = {"body": body, "accounts": survivors, "catalog": catalog,
                                      "issues": account_issues, "expires": now + 600,
                                      "fingerprint": _preflight_fingerprint(emails)}

        return jsonify({
            "ok": not blockers,
            "preflight_id": receipt_id,
            "can_remove_and_continue": reusable and bool(removable),
            "removable_accounts": sorted(removable),
            "provider": provider,
            "accounts": {"selected": len(emails), "validated": len(accounts)},
            "tasks": {"selected": len(raw_tasks), "compatible": len(selected)},
            "clips": clip_count,
            "estimated_sends": estimated_sends,
            "target_hours": target_hours,
            "blockers": blockers,
            "warnings": warnings,
            "account_errors": account_errors,
            "account_issues": account_issues,
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
        receipt = None
        receipt_id = body.get("preflight_id")
        if receipt_id:
            receipt = preflights.get(str(receipt_id))
            original = {k: v for k, v in body.items() if k not in {"preflight_id", "remove_restricted"}}
            if (receipt is None or receipt["expires"] <= time.monotonic()
                    or original != receipt["body"]
                    or receipt["fingerprint"] != _preflight_fingerprint(original.get("accounts", []))):
                return jsonify({"error": "A verificação expirou ou as contas mudaram. Execute o preflight novamente."}), 409
            if receipt["issues"] and body.get("remove_restricted") is not True:
                return jsonify({"error": "Confirme a remoção das contas com restrição para continuar."}), 400
        elif body.get("remove_restricted"):
            return jsonify({"error": "Execute o preflight antes de remover contas e continuar."}), 400
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
        if receipt:
            emails = [a.email for a in receipt["accounts"]]
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

        try:
            known = {a["email"] for a in _list_accounts()}
        except ValueError:
            return jsonify({"error": "Registro de contas banidas inválido. A campanha não iniciou."}), 500
        missing = [e for e in emails if e not in known]
        if missing:
            return jsonify({"error": "conta(s) sem token: " + ", ".join(missing)}), 400

        # O POST público continua seguro mesmo se um cliente antigo pular o
        # preflight. Sem ferramentas, índices ou credenciais da origem, iniciar
        # uma thread apenas produziria uma falha tardia depois do download.
        if receipt:
            accounts = receipt["accounts"]
            available = receipt["catalog"]
            skipped = []
        else:
            try:
                environment = readiness.campaign_readiness(dataset_provider)
            except (ValueError, OSError, RuntimeError) as exc:
                return jsonify({"error": f"não foi possível validar a prontidão: {exc}"}), 400
            environment_errors = [
                item for item in environment.get("checks", [])
                if item.get("status") == "error"
            ]
            if environment_errors:
                details = "; ".join(
                    f"{item.get('name')}: {item.get('detail')}"
                    for item in environment_errors
                )
                return jsonify({"error": "ambiente não está pronto: " + details}), 400

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
            if skipped:
                return jsonify({
                    "error": "campanha bloqueada: não foi possível validar o acesso "
                             "e a organização de todas as contas. " + "; ".join(skipped),
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

        others = [] if receipt else accounts[1:]
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
            if receipt and receipt["issues"]:
                if BALANCES_RUNNER.running:
                    return jsonify({"error": "Aguarde a consulta de saldos terminar."}), 409
                try:
                    _ban_accounts(receipt["issues"])
                except (OSError, ValueError) as exc:
                    return jsonify({"error": "Não foi possível registrar e remover as contas. A campanha não iniciou."}), 500
            try:
                RUNNER.start(cfg)
                if receipt_id:
                    preflights.pop(str(receipt_id), None)
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
            "removed_accounts": sorted({i["email"] for i in receipt["issues"]}) if receipt else [],
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
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return jsonify({"error": "log ilegível (JSON vazio/corrompido)"}), 400
        entries: list[tuple[str, str, str, int]] = []
        for it in data.get("items", []):
            for acc in it.get("accounts", []):
                sid = acc.get("session_id")
                if not sid:
                    continue
                uploads = acc.get("uploads")
                expected_files = len(uploads) if isinstance(uploads, list) else 1
                expected_files = max(1, expected_files)
                org = acc.get("org_key") or _safe_org(acc.get("email", ""))
                if not org:
                    entries.append((str(acc.get("email") or ""), "", str(sid),
                                    expected_files))
                    continue
                entries.append((str(acc.get("email") or ""), str(org), str(sid),
                                expected_files))

        # Uma sessão autenticada por conta evita renovar o mesmo refresh token
        # para cada vídeo. Contas diferentes são consultadas em paralelo; as
        # sessões da mesma conta continuam seriais para não disputar tokens.
        grouped: dict[str, list[tuple[str, str, int]]] = {}
        for email, org, sid, expected_files in entries:
            grouped.setdefault(email, []).append((org, sid, expected_files))

        def _check_account(
            group: tuple[str, list[tuple[str, str, int]]],
        ) -> list[dict[str, Any]]:
            email, account_entries = group
            output: list[dict[str, Any]] = []
            valid = [(org, sid, expected) for org, sid, expected in account_entries
                     if org]
            invalid = [(org, sid, expected) for org, sid, expected in account_entries
                       if not org]
            output.extend({"session_id": sid, "email": email,
                           "status": "sem org_key", "expected_files": expected}
                          for _, sid, expected in invalid)
            if not valid:
                return output
            try:
                sess = Session.from_email(email)
            except (AuthError, RuntimeError, OSError) as exc:
                return output + [
                    {"session_id": sid, "email": email,
                     "status": f"erro: {exc}", "expected_files": expected}
                    for _, sid, expected in valid]
            for org, sid, expected in valid:
                try:
                    result = campaign.session_result(
                        email, org, sid, session=sess)
                    result["expected_files"] = expected
                    output.append(result)
                except (AuthError, RuntimeError, OSError, json.JSONDecodeError) as exc:
                    output.append({"session_id": sid, "email": email,
                                   "status": f"erro: {exc}",
                                   "expected_files": expected})
            return output

        results: list[dict[str, Any]] = []
        groups = list(grouped.items())
        if groups:
            workers = min(6, len(groups))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for account_results in pool.map(_check_account, groups):
                    results.extend(account_results)
        session_ready = sum(item.get("status") == "preview_ready" for item in results)
        session_pending = sum(item.get("status") == "processing" for item in results)
        session_unavailable = sum(
            str(item.get("status") or "").startswith("unprocessed:unavailable")
            for item in results)
        session_errors = (len(results) - session_ready - session_pending
                          - session_unavailable)

        # A interface fala em prévias, portanto o progresso precisa contar os
        # arquivos/chunks reais, não apenas as sessões. Uma sessão longa pode
        # conter vários MP4s e pode estar parcialmente processada.
        ready = pending = unavailable = total = errors = transient_errors = 0
        for item in results:
            expected = max(1, int(item.get("expected_files") or 1))
            reported = max(0, int(item.get("total_files") or 0))
            status = str(item.get("status") or "")
            if status.startswith("erro:"):
                total += expected
                errors += expected
                transient_errors += expected
            elif status == "sem org_key":
                total += expected
                errors += expected
            else:
                item_total = max(expected, reported)
                item_ready = max(0, int(item.get("ready_files") or 0))
                item_pending = max(0, int(item.get("pending_files") or 0))
                item_unavailable = max(
                    0, int(item.get("unavailable_files") or 0))
                missing = max(
                    0, item_total - item_ready - item_pending - item_unavailable)
                total += item_total
                ready += item_ready
                pending += item_pending
                unavailable += item_unavailable
                if status == "processing":
                    pending += missing
                else:
                    errors += missing
        return jsonify({
            "results": results,
            "summary": {
                "total": total, "ready": ready, "pending": pending,
                "unavailable": unavailable, "errors": errors,
                "transient_errors": transient_errors,
            },
            "sessions": {
                "total": len(results), "ready": session_ready,
                "pending": session_pending, "unavailable": session_unavailable,
                "errors": session_errors,
            },
        })

    # -- saldos (crowtado) -----------------------------------------------------
    @app.get("/api/balances")
    def get_balances():
        configured = sorted(a["email"] for a in _list_accounts())
        configured_set = set(configured)
        # O cofre pode conservar credenciais de identidades removidas. Elas não
        # pertencem mais à operação atual e não devem inflar a contagem exibida
        # nem aparecer como contas conectadas no desktop.
        with_password = sorted(
            email for email in _configured_crowtado_creds() if email in configured_set
        )
        return jsonify({
            "balances": _load_balances(),
            "accounts": configured,
            "with_password": with_password,
            "runner": BALANCES_RUNNER.snapshot(),
            "exchange": fx.usd_brl_quote(),
        })

    @app.post("/api/balances/refresh")
    def refresh_balances():
        body = request.get_json(silent=True) or {}
        configured = {a["email"] for a in _list_accounts()}
        creds = {e: p for e, p in _configured_crowtado_creds().items() if e in configured}
        emails = [str(e).strip() for e in body.get("emails", []) if str(e).strip()]
        if emails:
            creds = {e: creds[e] for e in emails if e in creds}
        if not creds:
            return jsonify({
                "error": (
                    "a identidade está conectada ao Minute, mas o acesso ao "
                    "Crowtado ainda não foi informado; use Conectar Crowtado "
                    "na linha da conta"
                ),
            }), 400
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
        password = _configured_crowtado_creds().get(email)
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
        try:
            account_bans.require_not_banned(email)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        configured = {a["email"] for a in _list_accounts()}
        if email not in configured:
            return jsonify({"error": "essa identidade não está conectada ao QMoney"}), 404
        try:
            crowtado.login(email, password)
        except (crowtado.CrowtadoError, RuntimeError, OSError) as exc:
            return jsonify({
                "error": f"o Crowtado não aceitou esse acesso: {exc}",
            }), 400
        _save_crowtado_cred(email, password)
        return jsonify({
            "ok": True,
            "email": email,
            "message": "Acesso ao Crowtado confirmado e salvo.",
        })

    # -- registro de enviados ---------------------------------------------------
    @app.get("/api/sent")
    def get_sent():
        return jsonify({"sent": sent_registry.summary()})

    @app.post("/api/sent/reset")
    def reset_sent():
        if RUNNER.running:
            return jsonify({
                "error": "aguarde a campanha terminar antes de resetar a lista de vídeos usados",
            }), 409
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
