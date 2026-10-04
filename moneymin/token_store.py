"""Owner-checked session tokens with collision-free names and preserved legacy files."""
from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any

from .atomic_io import save_json

_LOCK = threading.RLock()
_CANONICAL = re.compile(r"token_[0-9a-f]{64}\.json")
_ERROR = "O acesso local está inválido ou possui identidade conflitante. Preserve os arquivos e revise o acesso salvo."


class TokenStoreError(ValueError):
    """An unreadable/conflicting record is never an absent or different account."""


@contextmanager
def transaction():
    """Serialize local snapshots/writes/rollback with all token-store writers.

    This reentrant lock covers one process. Acquire session/path locks before
    entering it; vault code does not acquire token or session locks.
    """
    with _LOCK:
        yield


def email_key(email: object) -> str:
    if not isinstance(email, str):
        raise TokenStoreError(_ERROR) from None
    key = email.strip().casefold()
    if len(key) > 200 or not re.fullmatch(r"[a-z0-9.!#$%&'+_=~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+", key):
        raise TokenStoreError(_ERROR) from None
    return key


def record_path(directory: Path, email: object) -> Path:
    return Path(directory) / ("token_" + hashlib.sha256(email_key(email).encode("utf-8")).hexdigest() + ".json")


def legacy_path(directory: Path, email: object) -> Path:
    key = email_key(email)
    return Path(directory) / ("token_" + key.replace("@", "_at_").replace(".", "_") + ".json")


def identity_uid(document: dict[str, Any]) -> str | None:
    values = []
    for key in ("localId", "user_id", "uid"):
        if key in document:
            value = document[key]
            if not isinstance(value, str) or not value.strip():
                raise TokenStoreError(_ERROR) from None
            values.append(value)
    if len(set(values)) > 1:
        raise TokenStoreError(_ERROR) from None
    return values[0] if values else None


def validated(document: Any, email: object | None = None, *, uid: str | None = None) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise TokenStoreError(_ERROR) from None
    owner = email_key(document.get("email"))
    if email is not None and owner != email_key(email):
        raise TokenStoreError(_ERROR) from None
    found_uid = identity_uid(document)
    if uid is not None and found_uid != uid:
        raise TokenStoreError(_ERROR) from None
    for key in ("idToken", "id_token", "refreshToken", "refresh_token"):
        if key in document and (not isinstance(document[key], str) or not document[key].strip()):
            raise TokenStoreError(_ERROR) from None
    return copy.deepcopy(document)


def validate_path(path: Path, email: object) -> None:
    path = Path(path)
    if _CANONICAL.fullmatch(path.name) and record_path(path.parent, email).name != path.name:
        raise TokenStoreError(_ERROR) from None


def read_file(path: Path, email: object | None = None, *, uid: str | None = None) -> dict[str, Any]:
    path = Path(path)
    try:
        if path.is_symlink():
            raise TokenStoreError(_ERROR)
        document = validated(json.loads(path.read_text(encoding="utf-8-sig")), email, uid=uid)
        validate_path(path, document["email"])
        return document
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise TokenStoreError(_ERROR) from None


def _present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _publish_exclusive(path: Path, payload: bytes, expected: dict) -> None:
    temporary = None
    try:
        # Validate the exact bytes copied, including a legacy changed after read.
        copied = validated(json.loads(payload.decode("utf-8-sig")), expected["email"],
                           uid=identity_uid(expected))
        if json.dumps(copied, sort_keys=True) != json.dumps(expected, sort_keys=True):
            raise TokenStoreError(_ERROR)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            # A concurrent login/migration owns the destination; never replace it.
            current = read_file(path, expected["email"], uid=identity_uid(expected))
            if json.dumps(current, sort_keys=True) != json.dumps(expected, sort_keys=True):
                raise TokenStoreError(_ERROR) from None
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise TokenStoreError(_ERROR) from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load(directory: Path, email: object, *, migrate: bool = True) -> tuple[Path, dict] | None:
    """A primary record is authoritative even when it is unreadable; no fallback."""
    key = email_key(email)
    primary = record_path(directory, key)
    with _LOCK:
        if _present(primary):
            return primary, read_file(primary, key)
        legacy = legacy_path(directory, key)
        candidates = []
        if _present(legacy):
            candidates.append((legacy, read_file(legacy, key)))
        for path in sorted(Path(directory).glob("token_*.json")):
            if path == legacy or _CANONICAL.fullmatch(path.name):
                continue
            try:
                document = read_file(path)
            except TokenStoreError:
                continue
            if email_key(document["email"]) == key:
                candidates.append((path, document))
        if not candidates:
            return None
        source, document = candidates[0]
        if any(json.dumps(other, sort_keys=True) != json.dumps(document, sort_keys=True) for _, other in candidates[1:]):
            raise TokenStoreError(_ERROR) from None
        if not migrate:
            return source, document
        try:
            payload = source.read_bytes()
        except OSError:
            raise TokenStoreError(_ERROR) from None
        _publish_exclusive(primary, payload, document)
        return primary, read_file(primary, key, uid=identity_uid(document))


def preflight_write(directory: Path, email: object) -> dict | None:
    """Fresh login may create its own primary without touching an ambiguous legacy."""
    path = record_path(directory, email)
    with _LOCK:
        return read_file(path, email) if _present(path) else None


def save(directory: Path, email: object, document: dict) -> Path:
    path = record_path(directory, email)
    with _LOCK:
        current = preflight_write(directory, email)
        candidate = validated(document, email, uid=identity_uid(current) if current else None)
        if current is None:
            try:
                payload = json.dumps(candidate, indent=2, ensure_ascii=False).encode("utf-8")
            except (TypeError, ValueError, RecursionError):
                raise TokenStoreError(_ERROR) from None
            _publish_exclusive(path, payload, candidate)
        else:
            save_json(path, candidate)
        return path


def save_file(path: Path, document: dict, email: object, *, uid: str | None = None) -> None:
    """Explicit file persistence preserves its demonstrated owner and known UID."""
    with _LOCK:
        candidate = validated(document, email, uid=uid)
        validate_path(path, email)
        if _present(Path(path)):
            current = read_file(path, email, uid=uid)
            validated(candidate, email, uid=identity_uid(current))
        if _present(Path(path)):
            save_json(Path(path), candidate)
        else:
            _publish_exclusive(Path(path), json.dumps(candidate, indent=2, ensure_ascii=False).encode("utf-8"), candidate)


def records(directory: Path, *, strict: bool = False) -> dict[str, tuple[Path, dict]]:
    """Prefer primaries; their mere presence hides every legacy copy of that owner."""
    with _LOCK:
        paths = sorted(Path(directory).glob("token_*.json"))
        result = {}
        conflicts = set()
        for path in paths:
            if not _CANONICAL.fullmatch(path.name):
                continue
            try:
                document = read_file(path)
                result[email_key(document["email"])] = (path, document)
            except TokenStoreError:
                if strict:
                    raise
        for path in paths:
            if _CANONICAL.fullmatch(path.name):
                continue
            try:
                document = read_file(path)
                key = email_key(document["email"])
            except TokenStoreError:
                continue
            if _present(record_path(directory, key)):
                continue
            if key in conflicts:
                continue
            if key in result and json.dumps(result[key][1], sort_keys=True) != json.dumps(document, sort_keys=True):
                if strict:
                    raise TokenStoreError(_ERROR) from None
                result.pop(key, None)
                conflicts.add(key)
                continue
            result.setdefault(key, (path, document))
        return result


def delete(directory: Path, email: object) -> None:
    """Delete only records whose payload proves the requested owner, never a name."""
    key = email_key(email)
    with _LOCK:
        primary = record_path(directory, key)
        selected = []
        if _present(primary):
            read_file(primary, key)
            selected.append(primary)
        for path in sorted(Path(directory).glob("token_*.json")):
            if _CANONICAL.fullmatch(path.name):
                continue
            try:
                document = read_file(path)
            except TokenStoreError:
                continue
            if email_key(document["email"]) == key:
                selected.append(path)
        for path in selected:
            path.unlink()
