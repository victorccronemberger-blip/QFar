"""Inspect persisted uploads and reconcile confirmed receipts without network I/O."""
from __future__ import annotations

import json
import threading

from . import campaign, sent_registry, upload
from .campaign_types import AccountSpec


def _groups() -> list[list[dict]]:
    groups: dict[tuple[str, str, str], list[dict]] = {}
    owners: dict[str, tuple[str, str]] = {}
    for path in sorted(upload.sidecars_dir().glob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("Um registro de envio está ilegível. Preserve os dados e revise a recuperação.") from exc
        if not isinstance(row, dict):
            raise ValueError("Um registro de envio tem formato inválido.")
        key = tuple(row.get(field) for field in ("account_email", "org_key", "session_id"))
        if any(not isinstance(value, str) or not value for value in key):
            raise ValueError("Um registro de envio não identifica a conta, organização ou sessão.")
        try:
            expected_path = upload._sidecar_path(key[2], row.get("chunk_index"))
        except upload.UploadError as exc:
            raise ValueError("Um registro de envio tem identidade inválida.") from exc
        if path.name != expected_path.name:
            raise ValueError("Um registro de envio não corresponde à sua sessão e parte.")
        if key[2] in owners and owners[key[2]] != key[:2]:
            raise ValueError("Uma sessão de envio tem identidades conflitantes.")
        owners[key[2]] = key[:2]
        groups.setdefault(key, []).append(row)
    return [rows for rows in groups.values() if not all(row.get("campaign_reconciled") is True for row in rows)]


def _describe(rows: list[dict]) -> dict | None:
    first = rows[0]
    sid, email = first["session_id"], first["account_email"]
    context = first.get("campaign_context") or campaign._legacy_upload_context(sid, email)
    identified = isinstance(context, dict) and bool(context.get("registry_key") and context.get("clip_uid"))
    if sent_registry.recovery_was_reset(sid, context.get("registry_key", "") if identified else "",
                                         context.get("history_name", "") if identified else ""):
        return None
    expected = first.get("expected_chunk_count", 1)
    indexes = [row.get("chunk_index") for row in rows]
    confirmed = (identified and type(expected) is int and expected > 0
                 and all(type(index) is int for index in indexes)
                 and len(rows) == expected and set(indexes) == set(range(expected))
                 and all(row.get("state") == "done" and row.get("finalized") is True
                         and row.get("expected_chunk_count", 1) == expected
                         and row.get("campaign_context") == first.get("campaign_context")
                         for row in rows))
    resumable = (identified and not confirmed and type(expected) is int and expected > 0
                 and all(type(index) is int for index in indexes)
                 and len(rows) == expected and set(indexes) == set(range(expected))
                 and all(row.get("state") in upload.TRANSIENT_STATES | {upload.STATE_LOSS, "done"}
                         and row.get("expected_chunk_count", 1) == expected
                         and row.get("task_id") == first.get("task_id")
                         and row.get("campaign_context") == first.get("campaign_context")
                         for row in rows)
                 and any(row.get("state") in upload.TRANSIENT_STATES | {upload.STATE_LOSS} for row in rows))
    return {
        "email": email, "session_id": sid,
        "clip_uid": context.get("clip_uid") if identified else None,
        "status": "confirmed" if confirmed else "pending" if resumable else "needs_review",
        "can_resume": bool(resumable),
        "chunks_found": len(rows), "chunks_expected": expected if type(expected) is int else None,
        "detail": "Finalização registrada; falta reconciliar a lista local." if confirmed else
                  "Envio interrompido; preserve a sessão existente antes de tentar novamente." if resumable else
                  "Registro incompleto ou sem retomada automática; preserve os arquivos e revise o histórico.",
    }


def snapshot() -> dict:
    items = [item for rows in _groups() if (item := _describe(rows)) is not None]
    return {"items": items, "pending": sum(item["status"] != "confirmed" for item in items),
            "confirmed": sum(item["status"] == "confirmed" for item in items)}


def reconcile_confirmed() -> dict:
    reconciled = 0
    for rows in _groups():
        item = _describe(rows)
        if item is None or item["status"] != "confirmed":
            continue
        first = rows[0]
        context = first.get("campaign_context") or campaign._legacy_upload_context(item["session_id"], item["email"])
        campaign._reconcile_uploads(rows, AccountSpec(item["email"], first["org_key"]),
                                   {"clip_uid": item["clip_uid"], "registry_key": context["registry_key"]},
                                   first.get("task_id", ""))
        reconciled += 1
    return {"reconciled": reconciled, **snapshot()}


def resume_account(email: str, resolve_org) -> dict:
    """Resume only reviewed, existing sessions of one authenticated account."""
    selected = []
    for rows in _groups():
        item = _describe(rows)
        if item and item["email"] == email and item["can_resume"]:
            selected.append(rows)
    if not selected:
        raise ValueError("Nenhuma sessão desta conta permite retomada automática.")
    org_key = resolve_org(email)
    if any(rows[0]["org_key"] != org_key for rows in selected):
        raise ValueError("A organização atual não corresponde aos envios pendentes.")
    session = campaign.Session.from_email(email)
    session.ensure_auth(org_key=org_key)
    upload.pump_pending(session, account_email=email, required_org_key=org_key,
                        session_ids={rows[0]["session_id"] for rows in selected})
    return reconcile_confirmed()


class RecoveryRunner:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = {"state": "idle", "email": None, "error": None}
        self._thread = None

    @property
    def running(self):
        with self._lock:
            return self._state["state"] == "running"

    def snapshot(self):
        with self._lock:
            return dict(self._state)

    def start(self, email: str, resolve_org):
        with self._lock:
            if self._state["state"] == "running":
                raise RuntimeError("Já existe uma recuperação em andamento.")
            self._state = {"state": "running", "email": email, "error": None}
            self._thread = threading.Thread(target=self._run, args=(email, resolve_org), daemon=True)
            try:
                self._thread.start()
            except Exception:
                self._state["state"] = "error"
                self._state["error"] = "Não foi possível iniciar a recuperação."
                raise

    def _run(self, email, resolve_org):
        try:
            result = resume_account(email, resolve_org)
            remains = any(item["email"] == email for item in result["items"])
            terminal = {"state": "pending" if remains else "done", "email": email, "error": None}
        except Exception:
            terminal = {"state": "error", "email": email,
                        "error": "A retomada não foi concluída. Confira o acesso da conta, a organização e os arquivos locais; os registros foram preservados."}
        with self._lock:
            self._state = terminal
