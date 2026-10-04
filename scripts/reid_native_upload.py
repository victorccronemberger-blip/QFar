#!/usr/bin/env python3
"""Entrada aposentada: reidentificação de capturas não preserva proveniência.

O fluxo histórico alterava IDs e datas ao reutilizar a mesma gravação em
outras contas. Sua implementação foi preservada como evidência fora do
projeto; esta entrada e as duas funções públicas falham antes de ler
capturas, carregar configuração/credenciais ou fazer chamadas de rede.

Uma importação válida precisa manter os originais, identidade, timestamps
e origem da captura. A implementação desse importador está no roadmap N02.
"""
from __future__ import annotations

import sys

_RETIRED_MESSAGE = (
    "Fluxo de reidentificação aposentado: preserve a identidade, as datas e "
    "os arquivos originais da captura. Consulte o roadmap N02 para importação "
    "com proveniência. Nenhum arquivo foi lido ou enviado por esta entrada."
)


class RetiredCaptureWorkflow(RuntimeError):
    """O consumidor deve migrar para importação que preserva os originais."""


def find_capture(session_id: str):
    """Compatibilidade de nome: falha sem consultar arquivos de captura."""
    raise RetiredCaptureWorkflow(_RETIRED_MESSAGE)


def reid_zip(orig_zip: bytes, new_sid: str, recorded_at: str,
             app_version: str = "1.28.0") -> bytes:
    """Compatibilidade de nome: falha sem reescrever identidade ou relógio."""
    raise RetiredCaptureWorkflow(_RETIRED_MESSAGE)


def main(argv: list[str] | None = None) -> int:
    """Retorna erro explícito; argumentos e seus possíveis segredos não são ecoados."""
    print(_RETIRED_MESSAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
