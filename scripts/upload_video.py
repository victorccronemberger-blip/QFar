#!/usr/bin/env python3
"""Retired standalone upload CLI; no configuration, file or provider access."""
from __future__ import annotations
import sys

_RETIRED_MESSAGE = (
    "CLI de envio avulso aposentada: use o fluxo revisado da Campanha ou "
    "a importação de captura original MP4 + ZIP, preservando identidade, "
    "timestamps e proveniência. Nenhum arquivo foi lido ou enviado."
)


def main(argv: list[str] | None = None) -> int:
    """Refuse without parsing or echoing private arguments."""
    print(_RETIRED_MESSAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
