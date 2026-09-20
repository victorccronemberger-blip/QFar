"""Armazenamento local e atômico de credenciais de contas.

Cada conta possui seu próprio registro. Assim, uma gravação ou processo
interrompido não consegue apagar a senha das outras contas ao substituir um
único JSON compartilhado.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .atomic_io import load_json, save_json

SCHEMA = 1
DIRECTORY = "crowtado_credentials"


def email_key(email: object) -> str:
    value = str(email or "").strip().casefold()
    if (len(value) > 200
            or not re.fullmatch(
                r"[a-z0-9.!#$%&'+_=~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+",
                value,
            )):
        raise ValueError("E-mail de credencial inválido.")
    return value


def record_path(secrets_dir: Path, email: object) -> Path:
    """Caminho determinístico do registro de uma conta normalizada."""
    key = email_key(email)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return secrets_dir / DIRECTORY / f"{digest}.json"


def _read(path: Path, expected_email: str | None = None) -> tuple[str, str] | None:
    raw = load_json(path, None)
    if not isinstance(raw, dict):
        return None
    try:
        email = email_key(raw.get("email"))
    except ValueError:
        return None
    password = raw.get("password")
    if (raw.get("schema") != SCHEMA or not isinstance(password, str)
            or not password or len(password) > 4096
            or (expected_email is not None and email != expected_email)):
        return None
    return email, password


def save(secrets_dir: Path, email: object, password: object) -> None:
    """Grava e relê a senha da conta antes de qualquer chamada externa."""
    key = email_key(email)
    if not isinstance(password, str) or not password or len(password) > 4096:
        raise ValueError("Senha de credencial inválida.")
    path = record_path(secrets_dir, key)
    existing = _read(path, key) if path.exists() else None
    if path.exists() and existing is None:
        raise ValueError("O registro local da credencial está inválido e foi preservado.")
    if existing is not None and existing[1] == password:
        return
    save_json(path, {"schema": SCHEMA, "email": key, "password": password})
    saved = _read(path, key)
    if saved is None or saved[1] != password:
        raise OSError("A credencial local não pôde ser confirmada após a gravação.")


def lookup(secrets_dir: Path, email: object, *, strict: bool = False) -> str | None:
    """Busca a senha sem confundir registro ausente com registro corrompido.

    No modo estrito, um arquivo existente mas ilegível/inconsistente bloqueia
    fallbacks legados que poderiam devolver uma senha antiga.
    """
    try:
        key = email_key(email)
    except ValueError:
        return None
    path = record_path(secrets_dir, key)
    found = _read(path, key)
    if strict and path.exists() and found is None:
        raise ValueError("O registro individual da credencial está inválido e foi preservado.")
    return found[1] if found is not None else None


def delete(secrets_dir: Path, email: object) -> None:
    """Remove somente o registro da conta solicitada."""
    path = record_path(secrets_dir, email)
    path.unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        # O diretório normalmente contém outras contas; removê-lo é opcional.
        pass


def load_all(secrets_dir: Path) -> dict[str, str]:
    directory = secrets_dir / DIRECTORY
    if not directory.is_dir():
        return {}
    result: dict[str, str] = {}
    for path in sorted(directory.glob("*.json")):
        found = _read(path)
        # O conteúdo não pode escolher a identidade de outro arquivo. Isso
        # impede que um arquivo renomeado/corrompido sobrescreva outra conta.
        if found is not None and path == record_path(secrets_dir, found[0]):
            result[found[0]] = found[1]
    return result


__all__ = [
    "DIRECTORY", "SCHEMA", "delete", "email_key", "load_all", "lookup",
    "record_path", "save",
]
