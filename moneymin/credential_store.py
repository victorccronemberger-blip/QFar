"""Armazenamento local, protegido por DPAPI e atômico de credenciais de contas.

Cada conta possui seu próprio registro. Assim, uma gravação ou processo
interrompido não consegue apagar a senha das outras contas ao substituir um
único JSON compartilhado.

Registros legados schema 1 só são migrados após validação. A migração substitui
o mesmo arquivo com um blob DPAPI conferido; não cria backup em texto puro.
"""
from __future__ import annotations

import hashlib
import re
import threading
from pathlib import Path
from .atomic_io import decode_json_state, save_bytes
from . import secure_store

SCHEMA = 2
LEGACY_SCHEMA = 1
DIRECTORY = "crowtado_credentials"
_MAGIC = b"QMoney credential DPAPI v2\x00"
_LOCK = threading.RLock()


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


def _read(path: Path, expected_email: str | None = None,
          *, strict_io: bool = False) -> tuple[str, str, bool] | None:
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        if strict_io:
            raise ValueError("O registro individual da credencial não pôde ser lido e foi preservado.") from None
        return None
    try:
        protected = payload.startswith(_MAGIC)
        raw = (secure_store.unprotect_json(payload[len(_MAGIC):]) if protected
               else decode_json_state(payload))
    except (OSError, RuntimeError, UnicodeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        email = email_key(raw.get("email"))
    except ValueError:
        return None
    password = raw.get("password")
    expected_schema = SCHEMA if protected else LEGACY_SCHEMA
    if (type(raw.get("schema")) is not int or raw["schema"] != expected_schema
            or not isinstance(password, str)
            or not password or len(password) > 4096
            or (expected_email is not None and email != expected_email)):
        return None
    return email, password, not protected


def _write(path: Path, email: str, password: str) -> None:
    # DPAPI round-trip happens before replace: unavailability, malformed output
    # or a different Windows user cannot destroy a readable legacy record.
    payload = _MAGIC + secure_store.protect_json({
        "schema": SCHEMA, "email": email, "password": password,
    })
    try:
        save_bytes(path, payload)
        saved = _read(path, email)
        if saved != (email, password, False):
            raise OSError
    except OSError:
        raise OSError("A credencial protegida não pôde ser confirmada após a gravação.") from None


def _migrate(path: Path, found: tuple[str, str, bool]) -> tuple[str, str, bool]:
    if found[2]:
        _write(path, found[0], found[1])
        return found[0], found[1], False
    return found


def save(secrets_dir: Path, email: object, password: object) -> None:
    """Grava e relê a senha da conta antes de qualquer chamada externa."""
    key = email_key(email)
    if not isinstance(password, str) or not password or len(password) > 4096:
        raise ValueError("Senha de credencial inválida.")
    path = record_path(secrets_dir, key)
    with _LOCK:
        existing = _read(path, key, strict_io=True)
        if path.exists() and existing is None:
            raise ValueError("O registro local da credencial está inválido e foi preservado.")
        if existing is not None and existing[1] == password and not existing[2]:
            return
        _write(path, key, password)


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
    with _LOCK:
        found = _read(path, key, strict_io=strict)
        if strict and path.exists() and found is None:
            raise ValueError("O registro individual da credencial está inválido e foi preservado.")
        if found is not None:
            try:
                found = _migrate(path, found)
            except (OSError, ValueError):
                if strict:
                    raise secure_store.SecureStoreError(
                        "Não foi possível migrar a credencial local para proteção "
                        "Windows. O arquivo foi preservado; tente novamente."
                    ) from None
                return None
        return found[1] if found is not None else None


def delete(secrets_dir: Path, email: object) -> None:
    """Remove somente o registro da conta solicitada."""
    path = record_path(secrets_dir, email)
    with _LOCK:
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
    with _LOCK:
        result: dict[str, str] = {}
        for path in sorted(directory.glob("*.json")):
            found = _read(path)
            # Validate the filename before migration: a renamed record must not
            # claim or rewrite another identity.
            if found is not None and path == record_path(secrets_dir, found[0]):
                try:
                    found = _migrate(path, found)
                except (OSError, ValueError):
                    continue
                result[found[0]] = found[1]
        return result


__all__ = [
    "DIRECTORY", "SCHEMA", "delete", "email_key", "load_all", "lookup",
    "record_path", "save",
]
