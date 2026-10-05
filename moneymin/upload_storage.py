"""Per-installation upload journals, with non-destructive 1.x migration."""
from __future__ import annotations

import threading
import re
import os
import json
import tempfile
from pathlib import Path

from . import config, token_store
from .atomic_io import load_json_state, save_json
from .recovery_errors import RecoveryReadError

_LOCK = threading.RLock()


def _copy_archive_without_overwrite(source: Path, destination: Path) -> None:
    payload = source.read_bytes()
    try:
        # Exclusive creation also handles another writer creating the archive
        # after our journal-existence check. Never replace an existing payload.
        with destination.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if destination.read_bytes() != payload:
            raise RecoveryReadError("archive_conflict", destination)
    # An interrupted new copy leaves the source and partial destination intact;
    # its journal and migration acknowledgment are written only after success.


def _journal_matches(path: Path, expected: dict) -> bool:
    try:
        actual = load_json_state(path, None)
    except (ValueError, OSError):
        raise RecoveryReadError("journal_unreadable", path) from None
    # Canonical JSON preserves value types; dict equality considers True == 1.
    return (isinstance(actual, dict)
            and json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True))


def _save_migrated_journal_without_overwrite(target: Path, row: dict) -> None:
    # Publish a complete JSON atomically without replacing a concurrent writer.
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.",
                                     suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        save_json(temporary, row)
        try:
            os.link(temporary, target)
        except FileExistsError:
            if not _journal_matches(target, row):
                raise RecoveryReadError("journal_conflict", target)
    finally:
        temporary.unlink(missing_ok=True)


def journal_directory() -> Path:
    destination = config.DATA_DIR / "sidecars"
    destination.mkdir(parents=True, exist_ok=True)
    legacy = config.MEDIA_DATA_DIR / "sidecars"
    if legacy.resolve() == destination.resolve() or not legacy.is_dir():
        return destination
    with _LOCK:
        # Account ownership comes only from this installation's tokens. Never
        # import another customer's journal just because a library is shared.
        owners = set(token_store.records(config.tokens_dir()))
        if not owners:
            return destination
        marker_path = config.DATA_DIR / "sidecar_migration.json"
        try:
            marker = load_json_state(marker_path, {})
        except (ValueError, OSError):
            raise RecoveryReadError("migration_marker") from None
        if not isinstance(marker, dict):
            raise RecoveryReadError("migration_marker")
        source_key = str(legacy.resolve())
        migrated = marker.get(source_key, [])
        if not isinstance(migrated, list) or any(not isinstance(name, str) for name in migrated):
            raise RecoveryReadError("migration_marker")
        imported = set(migrated)
        # Validate all consulted sources and existing destinations before
        # copying one archive or publishing one journal/acknowledgment. A later
        # ambiguous record must not leave an earlier partial migration behind.
        pending = []
        for path in legacy.glob("*.json"):
            if path.name in imported:
                continue
            try:
                row = load_json_state(path, {})
            except (ValueError, OSError):
                raise RecoveryReadError("migration_source", path) from None
            if not isinstance(row, dict) or str(row.get("account_email", "")).strip().casefold() not in owners:
                continue
            sid = row.get("session_id")
            chunk = row.get("chunk_index", 0)
            if (not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", sid)
                    or type(chunk) is not int or chunk < 0
                    or path.name != f"{sid}{'__' + str(chunk) if chunk else ''}.json"):
                raise RecoveryReadError("migration_source", path)
            target = destination / path.name
            archive = path.with_suffix(".data.zip")
            copied_archive = destination / archive.name
            if archive.exists():
                # Use the sibling archive, not an arbitrary persisted path.
                if archive.resolve().parent != legacy.resolve():
                    raise ValueError("Arquivo de retomada fora da biblioteca de origem.")
                row["sidecar_data_path"] = str(copied_archive.resolve())
            if target.exists() and not _journal_matches(target, row):
                raise RecoveryReadError("journal_conflict", target)
            if archive.exists() and copied_archive.exists() and archive.read_bytes() != copied_archive.read_bytes():
                raise RecoveryReadError("archive_conflict", copied_archive)
            pending.append((path, row, target, archive, copied_archive))
        for path, row, target, archive, copied_archive in pending:
            if archive.exists():
                _copy_archive_without_overwrite(archive, copied_archive)
            _save_migrated_journal_without_overwrite(target, row)
            imported.add(path.name)
        if imported != set(migrated):
            marker[source_key] = sorted(imported)
            save_json(marker_path, marker)
    return destination
