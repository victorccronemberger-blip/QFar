"""Primitivas pequenas para persistência local resistente a interrupções.

Arquivos de estado nunca são sobrescritos diretamente: o conteúdo completo é
gravado ao lado do destino e só então substituído de forma atômica. Isso evita
JSON truncado quando o processo ou o Windows é encerrado durante uma gravação.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_json(path: Path, default: Any) -> Any:
    """Lê JSON UTF-8 (com ou sem BOM); devolve ``default`` se estiver inválido."""
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, value: Any, *, ensure_ascii: bool = False) -> None:
    """Persiste JSON por replace atômico no mesmo diretório do destino."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=ensure_ascii),
        encoding="utf-8",
    )
    temporary.replace(path)


def save_bytes(path: Path, value: bytes) -> None:
    """Persiste bytes completos por replace atômico no mesmo diretório."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_bytes(value)
    temporary.replace(path)


__all__ = ["load_json", "save_bytes", "save_json"]
