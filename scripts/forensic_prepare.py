#!/usr/bin/env python3
"""Entrada aposentada: uma captura original não pode receber nova identidade.

O preparador histórico alterava IDs, datas, versão e relógios de um sidecar
mantendo os CSVs antigos. Os originais e derivados históricos devem ser
preservados; esta entrada não lê, prepara ou modifica nenhum artefato.
"""
from __future__ import annotations

import sys

_RETIRED_MESSAGE = (
    "Fluxo de preparação com reidentificação aposentado: preserve os arquivos, "
    "a identidade, as datas e a origem da captura. Para inspeção estrutural "
    "somente leitura, use scripts/inspect_original_capture.py com originais "
    "explicitamente indicados. A inspeção não comprova origem física nem "
    "aceitação pelo provedor. Nenhum arquivo foi lido, preparado ou enviado."
)


def main(argv: list[str] | None = None) -> int:
    """Mantém o alias público sem interpretar nem expor argumentos."""
    print(_RETIRED_MESSAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
