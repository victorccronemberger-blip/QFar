"""Inspect persisted uploads and reconcile confirmed receipts without network I/O."""
from __future__ import annotations

import threading
from pathlib import Path

from . import campaign, sent_registry, upload
from .atomic_io import save_json
from .recovery_errors import RecoveryReadError
from .media_lifecycle import media_state_lease
from .operation_lease import OperationLeaseError
from .upload_types import (is_pending_finalization, journal_delivery_confirmed,
                           journal_evaluation_confirmed, journal_flags_valid)

_UNREAD_PUBLICATION = object()


def media_cleanup_protection() -> dict:
    """Read every authoritative journal, including hidden/acknowledged rows.

    Unfinished groups and original source reservations survive cleanup. A bad
    store is an error, never an empty protection list. No journal is rewritten.
    """
    from . import campaign_start_store, original_capture
    groups = _groups(include_reconciled=True)
    paths, hashes = set(), set()
    for rows in groups:
        if any(not journal_flags_valid(row) for row in rows):
            raise ValueError('Registros de envio inválidos impedem a limpeza segura.')
        # Local ACK is not enough to discard bytes before the immutable
        # attempt/history has a corresponding published group.
        releasable = (_complete_chunk_group(rows) and all(
            journal_delivery_confirmed(row) and row.get('campaign_reconciled') is True for row in rows))
        if releasable:
            from .campaign_evidence import publication_index, publication_registered
            releasable = publication_registered(rows, publication_index())
        if not releasable:
            for row in rows:
                context = row.get('campaign_context')
                provenance = context.get('content_provenance') if isinstance(context,dict) else None
                if provenance is not None:
                    try:
                        from .content_provenance import canonical_digest
                        expected = provenance['delivery_binding_sha256']
                        if canonical_digest({k:v for k,v in provenance.items() if k!='delivery_binding_sha256'}) != expected:
                            raise ValueError
                        assets = provenance['content']['assets']
                        for asset in assets.values():
                            digest = asset['sha256']
                            if not isinstance(digest,str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
                                raise ValueError
                            hashes.add(digest)
                    except (KeyError,TypeError,ValueError):
                        raise ValueError('Vínculo de conteúdo inválido impede a limpeza segura.') from None
                for key in ('video_path', 'sidecar_data_path'):
                    value = row.get(key)
                    if value is not None:
                        if not isinstance(value,str) or not value or not Path(value).is_absolute():
                            raise ValueError('Vínculo de mídia inválido impede a limpeza segura.')
                        paths.add(Path(value).resolve())
                archive = upload._sidecar_archive_path(row['session_id'], row.get('chunk_index',0))
                paths.add(archive.resolve())
    reservations = original_capture._read_reservations()
    for row in reservations['bindings'].values():
        hashes.update(row['media_sha256']);hashes.update(row['sidecar_sha256'])
    paths.update(Path(value).resolve() for value in campaign_start_store.protected_paths())
    return {'paths': paths, 'sha256': hashes}


def _complete_chunk_group(rows: list[dict]) -> bool:
    if not rows:
        return False
    expected = rows[0].get("expected_chunk_count", 1)
    indexes = [row.get("chunk_index") for row in rows]
    return (type(expected) is int and expected > 0
            and all(journal_flags_valid(row) for row in rows)
            and all(type(row.get("expected_chunk_count", 1)) is int
                    and row.get("expected_chunk_count", 1) == expected for row in rows)
            and all(type(index) is int for index in indexes)
            and len(rows) == expected and set(indexes) == set(range(expected)))


def _groups(directory=None, *, include_reconciled=False,
            publication_index=_UNREAD_PUBLICATION) -> list[list[dict]]:
    groups: dict[tuple[str, str, str], list[dict]] = {}
    owners: dict[str, tuple[str, str]] = {}
    try:
        paths = sorted(path for path in (directory or upload.sidecars_dir()).iterdir()
                       if path.name.lower().endswith(".json"))
    except RecoveryReadError:
        raise
    except (OSError, ValueError):
        raise RecoveryReadError("store_unreadable") from None
    for path in paths:
        try:
            row = upload._read_sidecar_file(path)
        except (OSError, UnicodeError, ValueError, upload.UploadError):
            raise RecoveryReadError("journal_unreadable", path) from None
        if not isinstance(row, dict):
            raise RecoveryReadError("journal_unreadable", path)
        # Legacy zero-chunk journals are normalized only in memory. Listing
        # must not rewrite the original bytes merely to inspect recovery.
        row = {**row, "chunk_index": row.get("chunk_index", 0)}
        key = tuple(row.get(field) for field in ("account_email", "org_key", "session_id"))
        if any(not isinstance(value, str) or not value for value in key):
            raise RecoveryReadError("journal_identity", path)
        try:
            expected_name = upload._sidecar_filename(key[2], row.get("chunk_index"))
        except upload.UploadError:
            raise RecoveryReadError("journal_identity", path) from None
        if path.name != expected_name:
            raise RecoveryReadError("journal_identity", path)
        if key[2] in owners and owners[key[2]] != key[:2]:
            raise RecoveryReadError("session_conflict", path)
        owners[key[2]] = key[:2]
        groups.setdefault(key, []).append(row)
    if include_reconciled:
        return list(groups.values())
    if publication_index is _UNREAD_PUBLICATION:
        publication_index = _read_required_publications(list(groups.values()))
    return _visible_groups(list(groups.values()), publication_index)


def _read_required_publications(groups: list[list[dict]]):
    # A batch of unacknowledged legacy receipts already reads histories once
    # to resolve its context. No publication lookup is needed until ACK exists.
    if not any(_complete_chunk_group(rows) and all(
            row.get('campaign_reconciled') is True and journal_delivery_confirmed(row)
            for row in rows) for rows in groups):
        return None
    from .campaign_evidence import publication_index
    return publication_index()


def _visible_groups(groups: list[list[dict]], publications):
    from .campaign_evidence import publication_registered
    return [rows for rows in groups if not (
        _complete_chunk_group(rows)
        and all(row.get("campaign_reconciled") is True and journal_delivery_confirmed(row) for row in rows)
        and publication_registered(rows, publications))]


def _describe(rows: list[dict], legacy_contexts: dict | None = None, reset_checker=None,
              publication_index=_UNREAD_PUBLICATION) -> dict | None:
    first = rows[0]
    sid, email = first["session_id"], first["account_email"]
    context = first.get("campaign_context") or (legacy_contexts.get((sid, email))
        if legacy_contexts is not None else campaign._legacy_upload_context(sid, email))
    identified = (isinstance(context, dict)
                  and all(isinstance(context.get(key), str) and context[key] for key in ("registry_key", "clip_uid"))
                  and (not context.get("task_id") or context["task_id"] == first.get("task_id"))
                  and all(row.get("campaign_context") == first.get("campaign_context")
                          and row.get("task_id") == first.get("task_id") for row in rows))
    if (reset_checker or sent_registry.recovery_was_reset)(sid, context.get("registry_key", "") if identified else "",
                                         context.get("history_name", "") if identified else ""):
        return None
    expected = first.get("expected_chunk_count", 1)
    complete_group = _complete_chunk_group(rows)
    confirmed = (identified and complete_group
                 and all(journal_delivery_confirmed(row)
                         and row.get("campaign_context") == first.get("campaign_context")
                         for row in rows))
    index_reconciled = all(row.get('campaign_reconciled') is True for row in rows)
    publication_pending = False
    if confirmed and index_reconciled:
        from .campaign_evidence import publication_index as read_publications, publication_registered
        if publication_index is _UNREAD_PUBLICATION:
            publication_index = read_publications()
        publication_pending = not publication_registered(rows, publication_index)
    resumable = (identified and not confirmed and complete_group
                 and all(row.get("state") in upload.TRANSIENT_STATES | {upload.STATE_LOSS, "done"}
                         and (row.get("finalized") is not True or journal_delivery_confirmed(row))
                         and row.get("task_id") == first.get("task_id")
                         and row.get("campaign_context") == first.get("campaign_context")
                         for row in rows)
                 and any(row.get("state") in upload.TRANSIENT_STATES | {upload.STATE_LOSS}
                         or is_pending_finalization(row) for row in rows))
    if resumable:
        for row in rows:
            if row.get("state") == "done" and row.get("finalized") is True:
                continue
            if (row.get("state") not in upload.TRANSIENT_STATES | {upload.STATE_LOSS, "done"}
                    and not is_pending_finalization(row)):
                continue
            try:
                upload._pending_recovery_stage(row)
                if row.get("crash_resumes", 0) >= upload.MAX_CRASH_RESUMES:
                    resumable = False
                    break
            except upload.UploadError:
                resumable = False
                break
    return {
        "email": email, "session_id": sid,
        "clip_uid": context.get("clip_uid") if identified else None,
        "status": "confirmed" if confirmed else "pending" if resumable else "needs_review",
        "blocks_campaign": not identified,
        "can_resume": bool(resumable),
        "chunks_found": len(rows), "chunks_expected": expected if type(expected) is int else None,
        "index_reconciled": index_reconciled,
        "publication_pending": publication_pending,
        "detail": ("Envio finalizado e índice reconciliado; falta conferir o registro correspondente no Histórico. Os recibos foram preservados."
                   if publication_pending else "Finalização registrada; falta reconciliar a lista local.") if confirmed else
                  "Envio interrompido; preserve a sessão existente antes de tentar novamente." if resumable else
                  "Registro incompleto ou sem retomada automática; preserve os arquivos e revise o histórico.",
    }


def snapshot() -> dict:
    # Enumeration and reads share the writers/cleanup barrier. A disappearing
    # journal must not be mistaken for a corrupt store during local cleanup.
    try:
        with media_state_lease(wait=True):
            groups = _groups(include_reconciled=True)
    except OperationLeaseError:
        raise RecoveryReadError("busy") from None
    # History scans and archive inspection may be large. They use the copied
    # journal rows and must not hold the short checkpoint/cleanup barrier.
    publications = _read_required_publications(groups)
    groups = _visible_groups(groups, publications)
    missing = {(rows[0]["session_id"], rows[0]["account_email"]) for rows in groups
               if not rows[0].get("campaign_context")}
    contexts = campaign._legacy_upload_contexts(missing, [row for rows in groups for row in rows]) if missing else {}
    try:
        reset_checker = sent_registry.recovery_reset_checker()
    except (ValueError, OSError):
        raise RecoveryReadError("reset_history") from None
    items = [item for rows in groups if (item := _describe(rows, contexts, reset_checker, publications)) is not None]
    return {"items": items, "pending": sum(item["status"] in {"pending", "needs_review"} for item in items),
            "confirmed": sum(item["status"] == "confirmed" for item in items),
            "reconciliation_pending": sum(item['status'] == 'confirmed' and not item['index_reconciled'] for item in items),
            "publication_pending": sum(item['publication_pending'] for item in items)}


def campaign_exclusions(items: list[dict]) -> dict[str, list[str]]:
    """Reserve uncertain clips across categories without claiming delivery."""
    excluded: dict[str, set[str]] = {}
    for item in items:
        if item.get("clip_uid"):
            excluded.setdefault(item["clip_uid"], set()).add(item["email"])
    return {uid: sorted(emails) for uid, emails in excluded.items()}


def reconcile_confirmed() -> dict:
    directory = upload.sidecars_dir()
    groups = _groups(directory, include_reconciled=True)
    publications = _read_required_publications(groups)
    groups = _visible_groups(groups, publications)
    missing = {(rows[0]["session_id"], rows[0]["account_email"]) for rows in groups
               if not rows[0].get("campaign_context")}
    contexts = campaign._legacy_upload_contexts(missing, [row for rows in groups for row in rows]) if missing else {}
    confirmed = []
    deliveries = []
    for rows in groups:
        item = _describe(rows, contexts, publication_index=publications)
        if item is None or item["status"] != "confirmed":
            continue
        first = rows[0]
        context = first.get("campaign_context") or contexts[(item["session_id"], item["email"])]
        deliveries.append((context["registry_key"], item["clip_uid"], item["email"]))
        if any(row.get('campaign_reconciled') is not True for row in rows):
            confirmed.append(rows)
    # Commit the complete sent index first. An interrupted acknowledgment can
    # safely repeat; it never forgets a completed delivery or starts an upload.
    sent_registry.mark_sent_many(deliveries)
    for rows in confirmed:
        for row in rows:
            if row.get("campaign_reconciled") is True:
                continue
            save_json(directory / upload._sidecar_filename(row["session_id"], row["chunk_index"]),
                      {**row, "campaign_reconciled": True})
    return {"reconciled": len(confirmed), "reconciled_sessions": [
        {"email": rows[0]["account_email"], "session_id": rows[0]["session_id"],
         "clip_uid": (rows[0].get("campaign_context") or contexts[
             (rows[0]["session_id"], rows[0]["account_email"])])["clip_uid"]}
        for rows in confirmed], **snapshot()}


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
            owned = [item for item in result['items'] if item['email'] == email]
            remains = any(item['status'] != 'confirmed' or not item.get('index_reconciled') for item in owned)
            terminal = {"state": "pending" if remains else "done", "email": email, "error": None,
                        "result": {"reconciled": result.get("reconciled", 0),
                                   "reconciled_sessions": result.get("reconciled_sessions", []),
                                   "publication_pending": sum(item.get('publication_pending') is True for item in owned)}}
        except Exception:
            terminal = {"state": "error", "email": email,
                        "error": "A retomada não foi concluída. Confira o acesso da conta, a organização e os arquivos locais; os registros foram preservados."}
        with self._lock:
            self._state = terminal
