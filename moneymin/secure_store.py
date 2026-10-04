"""Cofre local de integrações protegido pelo DPAPI do Windows.

O arquivo criptografado pode acompanhar o perfil local do QMoney, mas só pode
ser aberto pelo mesmo usuário do Windows. Nenhum segredo é devolvido pelas APIs
de status ou gravado em JSON/texto puro.
"""
from __future__ import annotations

import ctypes
import json
import os
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Any

from .atomic_io import decode_json_state, save_bytes

_LOCK = threading.RLock()
_DESCRIPTION = "QMoney integrations v1"
_ENTROPY = b"QMoney::integrations::v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class SecureStoreError(ValueError):
    """O cofre existente não pode ser aberto com segurança."""


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _input_blob(value: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(value)
    blob = _DataBlob(
        len(value),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    return blob, buffer


def _crypt(value: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise RuntimeError("o cofre de integrações requer o Windows")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    source, source_buffer = _input_blob(value)
    entropy, entropy_buffer = _input_blob(_ENTROPY)
    output = _DataBlob()
    description = ctypes.c_wchar_p()
    if protect:
        function = crypt32.CryptProtectData
        function.argtypes = [
            ctypes.POINTER(_DataBlob), wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob), wintypes.LPVOID, wintypes.LPVOID,
            wintypes.DWORD, ctypes.POINTER(_DataBlob),
        ]
        function.restype = wintypes.BOOL
        ok = function(
            ctypes.byref(source), _DESCRIPTION, ctypes.byref(entropy),
            None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output),
        )
    else:
        function = crypt32.CryptUnprotectData
        function.argtypes = [
            ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(_DataBlob), wintypes.LPVOID, wintypes.LPVOID,
            wintypes.DWORD, ctypes.POINTER(_DataBlob),
        ]
        function.restype = wintypes.BOOL
        ok = function(
            ctypes.byref(source), ctypes.byref(description),
            ctypes.byref(entropy), None, None, _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output),
        )
    # Mantém os buffers vivos até a chamada Win32 retornar.
    del source_buffer, entropy_buffer
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)
        if description:
            kernel32.LocalFree(description)


def protect_json(value: dict[str, Any]) -> bytes:
    """Protege um objeto JSON pelo usuário Windows, sem fallback em texto puro.

    O blob é aberto e conferido antes de ser devolvido ao gravador. Falhas não
    incluem dados do payload nem a exceção original em logs/tracebacks.
    """
    try:
        if not isinstance(value, dict):
            raise TypeError
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                             allow_nan=False)
        encrypted = _crypt(payload.encode("utf-8"), protect=True)
        if unprotect_json(encrypted) != value:
            raise ValueError
        return encrypted
    except (OSError, RuntimeError, TypeError, ValueError, RecursionError):
        raise SecureStoreError(
            "Não foi possível proteger a credencial local. O arquivo anterior "
            "foi preservado; use o usuário Windows original e tente novamente."
        ) from None


def unprotect_json(payload: bytes) -> dict[str, Any]:
    """Abre um objeto protegido sem expor seu conteúdo em mensagens de erro."""
    try:
        if not isinstance(payload, bytes) or not payload:
            raise ValueError
        value = decode_json_state(_crypt(payload, protect=False).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, RuntimeError, TypeError, ValueError, RecursionError):
        raise SecureStoreError(
            "Não foi possível abrir a credencial protegida. O arquivo foi "
            "preservado; use o usuário Windows original ou um backup válido."
        ) from None


def load_secure_settings(path: Path, *, strict: bool = False) -> dict[str, Any]:
    with _LOCK:
        try:
            value = unprotect_json(path.read_bytes())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            if strict:
                raise SecureStoreError(
                    "Não foi possível abrir o cofre de integrações. O arquivo "
                    "foi preservado; use o usuário Windows original ou restaure "
                    "um backup válido."
                ) from None
            return {}
        return value


def save_secure_settings(path: Path, value: dict[str, Any]) -> None:
    with _LOCK:
        # Um cofre ilegível não é um cofre vazio. Não apague credenciais
        # existentes após corrupção ou cópia de outro usuário Windows.
        load_secure_settings(path, strict=True)
        # Valide formato, serialização e leitura do candidato protegido antes
        # de chegar ao replace atômico. Um JSON serializável pode ser uma lista
        # ou sofrer conversões incompatíveis com o objeto recebido.
        encrypted = protect_json(value)
        try:
            save_bytes(path, encrypted)
        except (OSError, RuntimeError, TypeError, ValueError):
            raise SecureStoreError(
                "Não foi possível salvar o cofre de integrações. O arquivo "
                "anterior foi preservado; verifique o acesso local e tente novamente."
            ) from None


def update_secure_section(path: Path, section: str,
                          value: dict[str, Any] | None) -> dict[str, Any]:
    if (not isinstance(section, str) or not section.strip() or section == "schema"
            or any(ord(character) < 32 for character in section)
            or value is not None and not isinstance(value, dict)):
        raise SecureStoreError("Seção de integração inválida; o cofre anterior foi preservado.") from None
    with _LOCK:
        settings = load_secure_settings(path, strict=True)
        settings["schema"] = 1
        if value:
            settings[section] = value
        else:
            settings.pop(section, None)
        save_secure_settings(path, settings)
        return settings


__all__ = ["SecureStoreError", "load_secure_settings", "protect_json",
           "save_secure_settings", "unprotect_json", "update_secure_section"]
