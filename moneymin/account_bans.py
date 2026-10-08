"""Permanent local exclusion and removal of account records from QMoney state."""
from __future__ import annotations

import json
from pathlib import Path

from . import banned_store, config, credential_store, token_store
from .atomic_io import save_json
from .jsonl_history import JsonlSyntaxError, decode_json_history_document, decode_jsonl_history


def banned_emails() -> set[str]:
    path = config.DATA_DIR / "banned_accounts.json"
    doc = banned_store.load(path, {"schema": 1, "accounts": []})
    return {str(row["email"]).strip().casefold() for row in doc["accounts"]}


def require_not_banned(email: str) -> None:
    if email.strip().casefold() in banned_emails():
        raise ValueError("Conta banida removida permanentemente; não pode ser reconectada ou importada.")


_DROP = object()

# These documents establish deliveries, media ownership or admission/reset
# decisions. A restriction removes usable account access, not the identity
# recorded by those decisions. Keep even unreadable documents byte-for-byte:
# their readers must remain able to diagnose an interrupted/corrupt operation.
_AUTHORITATIVE_STATE_NAMES = frozenset({
    "start_requests.json", "campaign_start_requests.json",
    "original_capture_reservations.json", "sent_videos.json",
    "sent_reset_history.json", "sidecar_migration.json",
})


def _is_authoritative_state(path: Path, roots: set[Path]) -> bool:
    return (path.name in _AUTHORITATIVE_STATE_NAMES
            or path.name.startswith("campaign_")
            or path.name.endswith((".managed.json", ".source.json"))
            or any(path.is_relative_to(root / "data" / "sidecars") for root in roots))


def _has_delivery_reference(value) -> bool:
    if isinstance(value, dict):
        return (bool({"video_path", "sidecar_data_path", "session_id"} & value.keys()) or
                any(_has_delivery_reference(child) for child in value.values()))
    if isinstance(value, list):
        return any(_has_delivery_reference(child) for child in value)
    return False


def _scrub(value, emails):
    if isinstance(value, str):
        return _DROP if value.strip().casefold() in emails else value
    if isinstance(value, dict):
        if str(value.get("email", "")).strip().casefold() in emails:
            return _DROP
        out = {}
        for key, child in value.items():
            if key.strip().casefold() in emails:
                continue
            cleaned = _scrub(child, emails)
            if cleaned is not _DROP:
                out[key] = cleaned
        return out
    if isinstance(value, list):
        out = [_scrub(child, emails) for child in value]
        return [child for child in out if child is not _DROP]
    return value


def purge_local_records(emails: set[str]) -> None:
    """Remove active account records; retain delivery evidence and media guards."""
    roots = {config.ROOT, config.LIBRARY_ROOT}
    # Token ownership and canonical-name conflicts must be checked by the store;
    # generic recursive scrubbing could otherwise erase a conflicting primary.
    for directory in {config.SECRETS_DIR, *(root / "secrets" for root in roots)}:
        for email in emails:
            token_store.delete(directory, email)
    for email in emails:
        credential_store.delete(config.SECRETS_DIR, email)
    paths = set()
    for root in roots:
        for directory in (root / "data", root / "secrets"):
            for pattern in ("*.json", "*.jsonl"):
                paths.update(directory.glob(pattern))
        for directory in (root / "recovery", root / "data/device_state"):
            if directory.exists():
                paths.update(directory.rglob("*.json"))
        paths.update(root.glob("*contas*.txt"))
    for path in sorted(paths):
        if path.name in {"banned_accounts.json", "removed_accounts.json"}:
            continue
        # Delivery records retain ownership and pending-media references even
        # when their account no longer has usable access. Never turn removal
        # into a reset of campaign/recovery evidence.
        if _is_authoritative_state(path, roots):
            continue
        if path.name.startswith("token_") and path.suffix == ".json":
            continue
        try:
            if path.suffix == ".jsonl":
                try:
                    # Preserve generic JSON roots and Unicode string contents.
                    # Preflight original bytes: decode only one initial BOM.
                    # Strict ambiguity errors propagate before this file is
                    # rewritten; unreadable legacy files stay intact as before.
                    rows = decode_jsonl_history(path.read_bytes())
                except JsonlSyntaxError:
                    continue
                if _has_delivery_reference(rows):
                    continue
                clean = _scrub(rows, emails)
                if clean != rows:
                    temporary = path.with_name(path.name + ".tmp")
                    temporary.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in clean), encoding="utf-8")
                    temporary.replace(path)
                continue
            raw = path.read_text(encoding="utf-8-sig")
            if path.suffix == ".txt":
                clean = "\n".join(line for line in raw.splitlines()
                                  if not any(email in line.casefold() for email in emails))
                if clean != raw.rstrip("\n"):
                    path.write_text(clean + "\n", encoding="utf-8")
                continue
            try:
                value = decode_json_history_document(raw)
            except JsonlSyntaxError:
                continue
            if _has_delivery_reference(value):
                continue
            clean = _scrub(value, emails)
            if clean is _DROP:
                path.unlink()
            elif clean != value:
                save_json(path, clean)
        except (UnicodeError, json.JSONDecodeError):
            # Arquivos que não são estado JSON legível não são reescritos.
            continue
