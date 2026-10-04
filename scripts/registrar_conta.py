#!/usr/bin/env python3
"""Retired registration CLI; retain only the existing strict JSONL helper."""
from __future__ import annotations
import sys

_RETIRED_MESSAGE = (
    "CLI de registro autônomo aposentada: gerencie suas contas próprias pela "
    "interface do aplicativo. Nenhuma configuração, credencial ou navegador "
    "foi acessado por esta entrada."
)


def main(argv: list[str] | None = None) -> int:
    """Refuse without parsing or echoing private arguments."""
    print(_RETIRED_MESSAGE, file=sys.stderr)
    return 2


# The CLI refuses before imports used exclusively by the retained helper.
if __name__ == "__main__":
    raise SystemExit(main())

import json
from moneymin import config
from moneymin.atomic_io import JsonStateError, decode_json_state
from moneymin.jsonl_history import decode_jsonl_history

CONTAS_PATH = config.DATA_DIR / "contas.jsonl"


def salvar_conta(registro: dict) -> None:
    # Validate the complete candidate/history before opening an append stream.
    # This preserves refused bytes, but is not a cross-process/crash transaction.
    try:
        serialized = json.dumps(registro, ensure_ascii=False)
        candidate = serialized.encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise JsonStateError("Estado local inválido ou ilegível. O arquivo foi preservado; revise ou restaure um backup válido antes de continuar.") from None
    decode_json_state(serialized)
    try:
        previous = CONTAS_PATH.read_bytes()
    except FileNotFoundError:
        previous = b""
    except OSError:
        raise JsonStateError("Estado local inválido ou ilegível. O arquivo foi preservado; revise ou restaure um backup válido antes de continuar.") from None
    decode_jsonl_history(previous, expected_type=dict)
    content = previous.removeprefix(b"\xef\xbb\xbf")
    separator = b"\n" if content.strip() and not previous.endswith(b"\n") else b""
    config.DATA_DIR.mkdir(exist_ok=True)
    with CONTAS_PATH.open("ab") as fh:
        fh.write(separator + candidate + b"\n")
    print(f"[+] conta salva em {CONTAS_PATH}")
