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

from .atomic_io import save_bytes

_LOCK = threading.RLock()
_DESCRIPTION = "QMoney integrations v1"
_ENTROPY = b"QMoney::integrations::v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


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


def load_secure_settings(path: Path) -> dict[str, Any]:
    with _LOCK:
        try:
            payload = _crypt(path.read_bytes(), protect=False)
            value = json.loads(payload.decode("utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}


def save_secure_settings(path: Path, value: dict[str, Any]) -> None:
    with _LOCK:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        save_bytes(path, _crypt(payload.encode("utf-8"), protect=True))


def update_secure_section(path: Path, section: str,
                          value: dict[str, Any] | None) -> dict[str, Any]:
    with _LOCK:
        settings = load_secure_settings(path)
        settings["schema"] = 1
        if value:
            settings[section] = value
        else:
            settings.pop(section, None)
        save_secure_settings(path, settings)
        return settings


__all__ = ["load_secure_settings", "save_secure_settings", "update_secure_section"]
