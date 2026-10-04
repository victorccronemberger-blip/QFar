"""Arquivo de contas arquivadas protegido por DPAPI, com leitura legada estrita.

O caminho e o objeto retornado permanecem compatíveis com o servidor. Um JSON
legado válido só é convertido na próxima gravação, após verificar o candidato
cifrado; leituras não reescrevem nem removem os dados existentes.
"""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any

from . import secure_store
from .atomic_io import save_bytes

_MAGIC = b"QMoney banned accounts DPAPI v1\x00"
_LOCK = threading.RLock()


class BannedStoreError(ValueError):
    """O arquivo de banidas não pôde ser lido ou preservado com segurança."""


def _validate(document: Any) -> dict[str, Any]:
    if (not isinstance(document, dict)
            or not isinstance(document.get("accounts"), list)
            or any(not isinstance(row, dict)
                   or not isinstance(row.get("email"), str)
                   or not row["email"].strip()
                   or (row.get("password") is not None
                       and not isinstance(row["password"], str))
                   for row in document["accounts"])):
        raise BannedStoreError(
            "O registro de contas banidas está inválido. O arquivo foi preservado."
        ) from None
    return document


def load(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    """Retorna o objeto em memória; só arquivo ausente admite o default.

    Corrupção, DPAPI indisponível ou falta de permissão nunca significam uma
    lista vazia de restrições. Nenhum valor aparece na mensagem/traceback.
    """
    with _LOCK:
        try:
            payload = path.read_bytes()
        except FileNotFoundError:
            if default is None:
                return {"schema": 1, "accounts": []}
            if not isinstance(default, dict):
                raise BannedStoreError("Default do registro de contas banidas inválido.") from None
            return copy.deepcopy(default)
        except OSError:
            raise BannedStoreError(
                "Não foi possível ler o registro de contas banidas. O arquivo foi preservado."
            ) from None
        try:
            document = (secure_store.unprotect_json(payload[len(_MAGIC):])
                        if payload.startswith(_MAGIC)
                        else json.loads(payload.decode("utf-8-sig")))
            return _validate(document)
        except (OSError, RuntimeError, UnicodeError, ValueError, RecursionError):
            raise BannedStoreError(
                "Não foi possível abrir o registro de contas banidas. O arquivo foi "
                "preservado; use o usuário Windows original ou um backup válido."
            ) from None


def save(path: Path, document: dict[str, Any]) -> None:
    """Protege e verifica antes do replace, sem substituir arquivo ilegível."""
    with _LOCK:
        _validate(document)
        # Existing corrupted/foreign-user data cannot be mistaken for empty.
        load(path)
        try:
            payload = _MAGIC + secure_store.protect_json(document)
        except (OSError, RuntimeError, ValueError, RecursionError):
            raise BannedStoreError(
                "Não foi possível proteger o registro de contas banidas. "
                "O arquivo anterior foi preservado; tente novamente no usuário Windows original."
            ) from None
        try:
            save_bytes(path, payload)
            if path.read_bytes() != payload or load(path) != document:
                raise ValueError
        except (OSError, ValueError):
            raise BannedStoreError(
                "Não foi possível confirmar a gravação protegida do registro de contas banidas."
            ) from None


__all__ = ["BannedStoreError", "load", "save"]
