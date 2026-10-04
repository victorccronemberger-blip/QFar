"""Strict line preflight for local JSONL history, without rewriting source bytes.

Historical syntax/UTF-8 failures are distinguishable from ambiguous JSON, so
generic purge can preserve unreadable files. Neither failure may become newly
authoritative credentials/state in a global account resolver.
"""
from __future__ import annotations

import json
from typing import Any

from .atomic_io import JsonStateError, decode_json_state

_ERROR = "Estado local inválido ou ilegível. O arquivo foi preservado; revise ou restaure um backup válido antes de continuar."


class JsonlSyntaxError(JsonStateError):
    """Historical JSON is unreadable, rather than ambiguous valid syntax."""


def decode_json_history_document(raw: str, *, expected_type: type | None = None) -> Any:
    """Decode exactly one strict JSON root while preserving generic root types."""
    try:
        # This guarantees exactly one root before a generic list wrapper. The
        # permissive result is never used for scrub, deletion or promotion.
        json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        raise JsonlSyntaxError(_ERROR) from None
    except (ValueError, RecursionError):
        raise JsonStateError(_ERROR) from None
    if expected_type is None:
        return decode_json_state("[" + raw + "]", expected_type=list)[0]
    return decode_json_state(raw, expected_type=expected_type)


def decode_jsonl_history(payload: bytes, *, expected_type: type | None = None) -> list[Any]:
    """Preflight every line before returning any record to a mutating caller.

    Byte framing accepts LF/CRLF/CR and an initial UTF-8 BOM; it never splits
    U+2028/U+2029 inside a JSON string. Generic history keeps its existing JSON
    root types via a list wrapper.
Error messages omit the original bytes, identities, paths and credentials.
"""
    records = []
    for encoded in payload.removeprefix(b"\xef\xbb\xbf").splitlines():
        try:
            line = encoded.decode("utf-8")
        except UnicodeError:
            raise JsonlSyntaxError(_ERROR) from None
        if line.strip():
            records.append(decode_json_history_document(line, expected_type=expected_type))
    return records
