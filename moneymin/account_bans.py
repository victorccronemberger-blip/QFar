"""Permanent local exclusion and removal of account records from QMoney state."""
from __future__ import annotations

import json
from pathlib import Path

from . import config, credential_store
from .atomic_io import save_json


def banned_emails() -> set[str]:
    path = config.DATA_DIR / "banned_accounts.json"
    if not path.exists():
        return set()
    doc = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(doc, dict) or not isinstance(doc.get("accounts"), list):
        raise ValueError("Registro de contas banidas inválido.")
    return {str(row["email"]).strip().casefold() for row in doc["accounts"]}


def require_not_banned(email: str) -> None:
    if email.strip().casefold() in banned_emails():
        raise ValueError("Conta banida removida permanentemente; não pode ser reconectada ou importada.")


_DROP = object()


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
    """Purga registros e backups geridos pelo app; mantém apenas banlist/tombstones."""
    for email in emails:
        credential_store.delete(config.SECRETS_DIR, email)
    roots = {config.ROOT, config.LIBRARY_ROOT}
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
        try:
            raw = path.read_text(encoding="utf-8-sig")
            if path.suffix == ".txt":
                clean = "\n".join(line for line in raw.splitlines()
                                  if not any(email in line.casefold() for email in emails))
                if clean != raw.rstrip("\n"):
                    path.write_text(clean + "\n", encoding="utf-8")
                continue
            if path.suffix == ".jsonl":
                rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
                clean = _scrub(rows, emails)
                if clean != rows:
                    temporary = path.with_name(path.name + ".tmp")
                    temporary.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in clean), encoding="utf-8")
                    temporary.replace(path)
                continue
            value = json.loads(raw)
            clean = _scrub(value, emails)
            if clean is _DROP:
                path.unlink()
            elif clean != value:
                save_json(path, clean)
        except (UnicodeError, json.JSONDecodeError):
            # Arquivos que não são estado JSON legível não são reescritos.
            continue
