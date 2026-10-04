"""Primitivas pequenas para persistência local resistente a interrupções.

Arquivos de estado nunca são sobrescritos diretamente: o conteúdo completo é
gravado ao lado do destino e só então substituído de forma atômica. Isso evita
JSON truncado quando o processo ou o Windows é encerrado durante uma gravação.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any


class JsonStateError(ValueError):
    """An authoritative JSON document cannot safely be treated as empty."""


def decode_json_state(raw: str | bytes, *, expected_type: type = dict) -> Any:
    """Decode authoritative JSON without performing I/O.

    Duplicate keys and nonfinite numbers are ambiguous state, not a cache miss.
    Error text and its displayed traceback omit payloads, paths and OS details.
    Consumers remain responsible for validating their nested schema.
    """
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError
        return number

    def invalid_constant(_value):
        raise ValueError

    try:
        result = json.loads(raw, object_pairs_hook=unique_keys, parse_float=finite_float,
                            parse_constant=invalid_constant)
        if type(result) is not expected_type:
            raise ValueError
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise JsonStateError(
            "Estado local inválido ou ilegível. O arquivo foi preservado; "
            "revise ou restaure um backup válido antes de continuar."
        ) from None


def load_json_state(path: Path, default: Any, *, expected_type: type = dict) -> Any:
    """Read authoritative state; only a missing file returns ``default``."""
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return default
    except (OSError, UnicodeError):
        raise JsonStateError(
            "Estado local inválido ou ilegível. O arquivo foi preservado; "
            "revise ou restaure um backup válido antes de continuar."
        ) from None
    return decode_json_state(raw, expected_type=expected_type)


def decode_json_value_state(raw: str) -> Any:
    """Decode exactly one unambiguous finite JSON value of any root type."""
    try:
        # A wrapper alone could turn comma-separated roots into a valid list.
        # This parse proves single-root grammar; its permissive value is unused.
        json.loads(raw)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise JsonStateError(
            "Estado local inválido ou ilegível. O arquivo foi preservado; "
            "revise ou restaure um backup válido antes de continuar."
        ) from None
    return decode_json_state("[" + raw + "]", expected_type=list)[0]


def load_json(path: Path, default: Any) -> Any:
    """Read a generic finite JSON cache; invalid/unreadable returns ``default``."""
    try:
        return decode_json_value_state(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, JsonStateError):
        return default


def save_json(path: Path, value: Any, *, ensure_ascii: bool = False) -> None:
    """Persiste JSON por replace atômico no mesmo diretório do destino."""
    save_bytes(path, json.dumps(value, indent=2, ensure_ascii=ensure_ascii).encode("utf-8"))


def save_bytes(path: Path, value: bytes) -> None:
    """Persiste bytes completos por replace atômico no mesmo diretório."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        # Each writer owns its temporary file; close before replace on Windows.
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(5):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                # Windows can briefly hold the destination during another replace.
                if attempt == 4:
                    raise
                time.sleep(0.01 * (attempt + 1))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


__all__ = ["JsonStateError", "decode_json_state", "decode_json_value_state", "load_json", "load_json_state", "save_bytes", "save_json"]
