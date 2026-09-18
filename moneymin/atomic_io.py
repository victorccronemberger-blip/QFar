"""Primitivas pequenas para persistência local resistente a interrupções.

Arquivos de estado nunca são sobrescritos diretamente: o conteúdo completo é
gravado ao lado do destino e só então substituído de forma atômica. Isso evita
JSON truncado quando o processo ou o Windows é encerrado durante uma gravação.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any


def load_json(path: Path, default: Any) -> Any:
    """Lê JSON UTF-8 (com ou sem BOM); devolve ``default`` se estiver inválido."""
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
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


__all__ = ["load_json", "save_bytes", "save_json"]
