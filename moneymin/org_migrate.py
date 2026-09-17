"""Troca o código de organização Minute das contas de um JSON Crowtado/QMoney.

Lê o backup `qmoney-accounts` (ou uma lista `{email, password|senha, token?}`)
e faz o equivalente a Configurações → código da org no app Minute:
`POST /api/v1/organizations/join`.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import account_transfer, config, minute_api, org_policy
from .atomic_io import load_json, save_json
from .web.account_issues import account_issue

PREFS_PATH = config.DATA_DIR / "webui_prefs.json"


def _orgs(profile: dict[str, Any] | None) -> list[dict[str, Any]]:
    items = (profile or {}).get("organizations") or []
    return [item for item in items if isinstance(item, dict)]


def _keys(orgs: list[dict[str, Any]]) -> set[str]:
    return {str(item.get("resourceKey") or "") for item in orgs if item.get("resourceKey")}


def _summarize_orgs(orgs: list[dict[str, Any]]) -> list[dict[str, str]]:
    out = []
    for item in orgs:
        key = str(item.get("resourceKey") or "")
        name = str(item.get("name") or "")
        if key or name:
            out.append({"name": name, "key": key})
    return out


def _token_payload(data: dict[str, Any], email: str) -> dict[str, Any]:
    payload = {"email": email}
    for key in ("idToken", "refreshToken", "localId", "expiresIn"):
        value = data.get(key)
        if value is not None:
            payload[key] = value
    payload["expires_at"] = data.get("expires_at", 0)
    return payload


def _set_pref_org(email: str, org_key: str) -> None:
    prefs = load_json(PREFS_PATH, {}) if PREFS_PATH.exists() else {}
    if not isinstance(prefs, dict):
        prefs = {}
    prefs.setdefault("org_keys", {})[email] = org_key
    save_json(PREFS_PATH, prefs)


def session_from_record(record: dict[str, Any]) -> minute_api.Session:
    """Abre sessão a partir do token do JSON; cai na senha se o token morrer."""
    email = record["email"]
    password = record.get("password")
    token = record.get("token")
    path = config.token_path(email)
    last: Exception | None = None
    if isinstance(token, dict) and token.get("refreshToken") and token.get("idToken"):
        sess = minute_api.Session(dict(token), token_file=path, email=email)
        try:
            sess.ensure_auth()
            return sess
        except (minute_api.AuthError, RuntimeError) as exc:
            last = exc
    if isinstance(password, str) and password:
        minute_api.login(email, password)
        return minute_api.Session.from_email(email)
    if last is not None:
        raise last
    raise minute_api.AuthError(
        f"{email}: o JSON não tem token válido nem senha para autenticar"
    )


def migrate_one(
    record: dict[str, Any],
    *,
    code: str,
    org_key: str,
    dry_run: bool = False,
    persist_prefs: bool = True,
) -> dict[str, Any]:
    """Migra uma conta, isolando falhas de rede e persistência por registro.

    O token de sucesso é interno e removido por migrate_file do relatório.
    """
    try:
        return _migrate_one(
            record, code=code, org_key=org_key,
            dry_run=dry_run, persist_prefs=persist_prefs,
        )
    except Exception as exc:  # noqa: BLE001 — uma conta não mata o lote
        issue = account_issue(record["email"], exc, stage="Migração da organização")
        return {
            "email": record["email"],
            "status": "restricted" if issue["code"] == "restricted" else "error",
            "reason": issue["reason"],
            "detail": issue["detail"],
        }


def _migrate_one(
    record: dict[str, Any],
    *,
    code: str,
    org_key: str,
    dry_run: bool,
    persist_prefs: bool,
) -> dict[str, Any]:
    email = record["email"]
    row: dict[str, Any] = {"email": email, "status": "error"}
    if dry_run:
        row["status"] = "dry_run"
        return row
    try:
        sess = session_from_record(record)
        profile = sess.ensure_auth()
    except Exception as exc:  # noqa: BLE001 — uma conta não mata o lote
        issue = account_issue(email, exc, stage="Login Minute")
        row["status"] = "restricted" if issue["code"] == "restricted" else "error"
        row["reason"] = issue["reason"]
        row["detail"] = issue["detail"]
        return row
    orgs = _orgs(profile if isinstance(profile, dict) else {})
    row["orgs_antes"] = _summarize_orgs(orgs)
    if org_key and org_key in _keys(orgs):
        if persist_prefs:
            _set_pref_org(email, org_key)
        row["status"] = "already"
        row["org_key"] = org_key
        row["token"] = _token_payload(sess.data, email)
        return row
    try:
        status, body = sess.join_org(code)
    except Exception as exc:  # noqa: BLE001
        issue = account_issue(email, exc, stage="Join da organização")
        row["status"] = "error"
        row["reason"] = issue["reason"]
        row["detail"] = issue["detail"]
        return row
    row["join_status"] = status
    if status not in (200, 201):
        row["status"] = "join_failed"
        row["detail"] = f"HTTP {status}"
        return row
    me = sess.me()
    after = _orgs(me if isinstance(me, dict) else {})
    row["orgs_depois"] = _summarize_orgs(after)
    joined = org_key in _keys(after) if org_key else bool(after)
    if not joined and isinstance(body, str):
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            parsed = {}
        got = ((parsed.get("organization") or {}).get("resourceKey")
               if isinstance(parsed, dict) else None)
        joined = bool(org_key) and got == org_key
    if joined:
        if persist_prefs:
            _set_pref_org(email, org_key)
        row["status"] = "migrated"
        row["org_key"] = org_key
        row["token"] = _token_payload(sess.data, email)
        return row
    row["status"] = "join_no_org"
    row["detail"] = "join respondeu mas a org nova não apareceu no perfil"
    return row


def load_records(path: Path) -> list[dict[str, Any]]:
    raw = path.read_text(encoding="utf-8-sig")
    records = []
    for index, item in enumerate(account_transfer.decode_document(raw), 1):
        try:
            records.append(account_transfer.clean_record(item))
        except ValueError as exc:
            records.append({
                "email": str(item.get("email") or f"linha-{index}")
                if isinstance(item, dict) else f"linha-{index}",
                "invalid": str(exc),
            })
    return records


def migrate_file(
    path: Path,
    *,
    code: str | None = None,
    org_key: str | None = None,
    dry_run: bool = False,
    write_json: bool = False,
    delay_s: float = 0.3,
    on_progress: Callable[[int, int, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Processa contas usando a política por e-mail, salvo destino explícito.

    O relatório não inclui segredos. Destinos mistos têm code/org_key nulos
    no resumo; cada resultado válido informa seu próprio destino.
    """
    records = load_records(path)
    results: list[dict[str, Any]] = []
    invites: set[str] = set()
    targets: set[str] = set()
    tokens_by_email: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(records, 1):
        if record.get("invalid"):
            row = {
                "email": record.get("email"),
                "status": "invalid",
                "reason": "Registro do JSON inválido.",
                "detail": "ValueError",
            }
        else:
            invite = (code or org_policy.target_invite(record["email"])).strip()
            target = (org_key or org_policy.target_org_key(record["email"])).strip()
            invites.add(invite)
            targets.add(target)
            row = migrate_one(
                record, code=invite, org_key=target,
                dry_run=dry_run,
            )
            token = row.pop("token", None)
            if isinstance(token, dict):
                tokens_by_email[row["email"]] = token
            row.update(code=invite, org_key=target)
        results.append(row)
        if on_progress:
            on_progress(index, len(records), row)
        if index < len(records) and delay_s > 0 and not dry_run:
            time.sleep(delay_s)
    if write_json and tokens_by_email and not dry_run:
        _rewrite_tokens(path, tokens_by_email)
    counts: dict[str, int] = {}
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "ok": True,
        "file": str(path),
        "code": next(iter(invites)) if len(invites) == 1 else None,
        "org_key": next(iter(targets)) if len(targets) == 1 else None,
        "dry_run": dry_run,
        "total": len(results),
        "counts": counts,
        "results": results,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _rewrite_tokens(path: Path, tokens_by_email: dict[str, dict[str, Any]]) -> None:
    doc = json.loads(path.read_text(encoding="utf-8-sig"))
    accounts = doc.get("accounts") if isinstance(doc, dict) else doc
    if not isinstance(accounts, list):
        return
    wanted = {email.casefold(): token for email, token in tokens_by_email.items()}
    for item in accounts:
        if not isinstance(item, dict):
            continue
        email = str(item.get("email") or "").strip().casefold()
        token = wanted.get(email)
        if token:
            item["token"] = token
    if isinstance(doc, dict):
        doc["exported_at"] = datetime.now(timezone.utc).isoformat()
        payload = doc
    else:
        payload = accounts
    path.write_text(
        json.dumps(payload, indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def write_report(report: dict[str, Any], dest: Path | None = None) -> Path:
    target = dest or (
        config.DATA_DIR
        / f"migrar_org_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return target
