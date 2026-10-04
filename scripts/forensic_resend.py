#!/usr/bin/env python3
"""Entrada aposentada: derivados reidentificados não são aquisição original.

O consumidor histórico enviava o manifesto com identidade e datas trocadas
pelo preparador. Os manifestos, recibos e artefatos legados permanecem como
evidência histórica; esta entrada não lê, autentica, envia ou finaliza.
"""
from __future__ import annotations

import sys

_RETIRED_MESSAGE = (
    "Fluxo de reenvio de captura reidentificada aposentado: preserve originais, "
    "manifestos, recibos e histórico. Para inspeção estrutural somente leitura, "
    "use scripts/inspect_original_capture.py com originais explicitamente "
    "indicados. A inspeção não comprova origem física nem aceitação pelo "
    "provedor. Nenhum arquivo foi lido ou enviado por esta entrada."
)


def main(argv: list[str] | None = None) -> int:
    """Mantém o alias público sem interpretar nem expor argumentos."""
    print(_RETIRED_MESSAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
