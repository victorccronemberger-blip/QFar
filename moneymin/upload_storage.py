"""Per-installation upload journals, with non-destructive 1.x migration."""
from __future__ import annotations

import threading
import re
from pathlib import Path

from . import config
from .atomic_io import load_json, save_bytes, save_json

_LOCK = threading.RLock()


def journal_directory() -> Path:
    destination = config.DATA_DIR / "sidecars"
    destination.mkdir(parents=True, exist_ok=True)
    legacy = config.MEDIA_DATA_DIR / "sidecars"
    if legacy.resolve() == destination.resolve() or not legacy.is_dir():
        return destination
    with _LOCK:
        # Account ownership comes only from this installation's tokens. Never
        # import another customer's journal just because a library is shared.
        owners = set()
        for path in config.tokens_dir().glob("token_*.json"):
            token = load_json(path, {})
            if isinstance(token, dict) and isinstance(token.get("email"), str):
                owners.add(token["email"].strip().casefold())
        if not owners:
            return destination
        marker_path = config.DATA_DIR / "sidecar_migration.json"
        marker = load_json(marker_path, None) if marker_path.exists() else {}
        if not isinstance(marker, dict):
            raise ValueError("Registro de migração de envios inválido; restaure o arquivo antes de continuar.")
        source_key = str(legacy.resolve())
        migrated = marker.get(source_key, [])
        if not isinstance(migrated, list) or any(not isinstance(name, str) for name in migrated):
            raise ValueError("Registro de migração de envios inválido.")
        imported = set(migrated)
        for path in legacy.glob("*.json"):
            if path.name in imported:
                continue
            row = load_json(path, {})
            if not isinstance(row, dict) or str(row.get("account_email", "")).strip().casefold() not in owners:
                continue
            sid = row.get("session_id")
            chunk = row.get("chunk_index", 0)
            if (not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", sid)
                    or type(chunk) is not int or chunk < 0
                    or path.name != f"{sid}{'__' + str(chunk) if chunk else ''}.json"):
                raise ValueError("Registro legado de envio inconsistente; restaure-o antes de continuar.")
            target = destination / path.name
            if not target.exists():
                archive = path.with_suffix(".data.zip")
                if archive.exists():
                    # Use the sibling archive, not an arbitrary persisted path.
                    if archive.resolve().parent != legacy.resolve():
                        raise ValueError("Arquivo de retomada fora da biblioteca de origem.")
                    copied_archive = destination / archive.name
                    save_bytes(copied_archive, archive.read_bytes())
                    row["sidecar_data_path"] = str(copied_archive.resolve())
                save_json(target, row)
            imported.add(path.name)
        if imported != set(migrated):
            marker[source_key] = sorted(imported)
            save_json(marker_path, marker)
    return destination
