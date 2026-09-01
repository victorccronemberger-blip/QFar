"""
framing.py — Enquadramento: mapear um vídeo documentado de dataset para a
categoria/tarefa correspondente do app Minute.

Modela a anotação no padrão EPIC-KITCHENS (tríade Verbo+Substantivo) e produz um
"manifesto de enquadramento" — o registro que liga um clipe local à categoria do
Minute, com as métricas de qualidade que o app avalia (Clarity, Variety, Task).

As categorias reais do Minute vêm da API (`Session.categories()`); aqui há um
casador heurístico por texto para sugerir o encaixe, que deve ser conferido.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import config

# Métricas de qualidade avaliadas pelo Minute (docs/EPIC_KITCHENS_100_SPEC.md).
QUALITY_METRICS = ("clarity", "variety", "task")


@dataclass
class ClipAnnotation:
    """Anotação de um segmento de vídeo no padrão EPIC-KITCHENS."""

    narration: str = ""            # ex.: "brew coffee in coffee maker"
    verb: str = ""                 # ex.: "brew"
    noun: str = ""                 # ex.: "coffee"
    narration_id: str = ""         # ex.: "P24_01_18"
    participant_id: str = ""       # ex.: "P24"
    video_id: str = ""             # ex.: "P24_01"
    start_timestamp: str = ""      # "HH:MM:SS.ms"
    stop_timestamp: str = ""       # "HH:MM:SS.ms"


@dataclass
class FramingRecord:
    """Liga um clipe local a uma categoria/tarefa do Minute."""

    video_path: str
    dataset: str
    annotation: ClipAnnotation = field(default_factory=ClipAnnotation)
    minute_category_id: str | None = None
    minute_category_name: str | None = None
    minute_task_id: str | None = None
    match_confidence: float = 0.0
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Palavras genéricas de instrução que não ajudam a distinguir tarefas.
_STOPWORDS = {
    "a", "an", "the", "in", "on", "of", "to", "and", "with", "your", "for", "or",
    "natural", "start", "finish", "record", "another", "then", "area", "into",
}


def _norm(text: str) -> set[str]:
    return {
        t
        for t in text.lower().replace("/", " ").replace("-", " ").split()
        if len(t) > 2 and t not in _STOPWORDS
    }


def _task_text(task: dict[str, Any]) -> str:
    tags = " ".join(task.get("tags", []) or [])
    cats = " ".join(c.get("label", "") for c in (task.get("categories") or []))
    # nome pesa mais: repetido para dar mais peso no overlap.
    return f"{task.get('name','')} {task.get('name','')} {task.get('description','')} {tags} {cats}"


def match_task(
    annotation: ClipAnnotation, tasks: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, float]:
    """Encontra a task do Minute que melhor casa com a anotação.

    Compara verbo/substantivo/narração com nome+descrição+tags+categoria de cada
    task (`Session.all_tasks(org_key)`). Devolve (task, confiança 0..1). Um nome
    de task que contenha verbo e substantivo tende a cravar confiança ~1.0.
    """
    query = _norm(f"{annotation.verb} {annotation.noun} {annotation.narration}")
    if not query:
        return None, 0.0
    best, best_score = None, 0.0
    for task in tasks:
        tokens = _norm(_task_text(task))
        if not tokens:
            continue
        overlap = len(query & tokens) / len(query)
        # bônus se o nome da task contém verbo e substantivo (encaixe direto).
        name_tokens = _norm(task.get("name", ""))
        if annotation.verb.lower() in name_tokens and annotation.noun.lower() in name_tokens:
            overlap = min(1.0, overlap + 0.5)
        if overlap > best_score:
            best, best_score = task, overlap
    return best, round(best_score, 3)


def match_category(
    annotation: ClipAnnotation, categories: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, float]:
    """Sugere a melhor categoria do Minute para uma anotação.

    Heurística por sobreposição de tokens entre verbo/substantivo/narração e o
    nome/descrição de cada categoria. Devolve (categoria, confiança 0..1).
    `categories` é a lista retornada por `Session.categories()`.
    """
    query = _norm(f"{annotation.verb} {annotation.noun} {annotation.narration}")
    if not query:
        return None, 0.0

    best, best_score = None, 0.0
    for cat in categories:
        label = f"{cat.get('name', '')} {cat.get('description', '')}"
        tokens = _norm(label)
        if not tokens:
            continue
        overlap = len(query & tokens) / len(query)
        if overlap > best_score:
            best, best_score = cat, overlap
    return best, round(best_score, 3)


def frame_clip(
    video_path: str | Path,
    dataset: str,
    annotation: ClipAnnotation,
    categories: list[dict[str, Any]] | None = None,
) -> FramingRecord:
    """Constrói o FramingRecord de um clipe, sugerindo a categoria se possível."""
    record = FramingRecord(
        video_path=str(video_path), dataset=dataset, annotation=annotation
    )
    if categories:
        cat, conf = match_category(annotation, categories)
        if cat:
            record.minute_category_id = str(cat.get("id") or cat.get("resourceKey") or "")
            record.minute_category_name = cat.get("name")
            record.match_confidence = conf
            if conf < 0.5:
                record.notes = "confiança baixa — conferir manualmente"
    return record


def frame_against_tasks(
    video_path: str | Path,
    dataset: str,
    annotation: ClipAnnotation,
    tasks: list[dict[str, Any]],
) -> tuple[FramingRecord, dict[str, Any] | None]:
    """Enquadra um clipe contra as tasks reais de uma org do Minute.

    Devolve (FramingRecord, task_casada). A task casada traz os dados completos
    (descrição, instruções, limitMs...) para montar a documentação.
    """
    record = FramingRecord(
        video_path=str(video_path), dataset=dataset, annotation=annotation
    )
    task, conf = match_task(annotation, tasks)
    if task:
        cats = task.get("categories") or [{}]
        record.minute_task_id = task.get("id")
        record.minute_category_id = cats[0].get("slug")
        record.minute_category_name = cats[0].get("label") or task.get("name")
        record.match_confidence = conf
        record.notes = (
            "encaixe perfeito (nome da task homônimo)" if conf >= 0.95
            else "conferir manualmente" if conf < 0.5 else ""
        )
    return record, task


# --- Manifesto ---------------------------------------------------------------
def save_manifest(records: list[FramingRecord], name: str = "framing_manifest.json") -> Path:
    """Grava os registros de enquadramento em `data/manifests/<name>`."""
    config.MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.MANIFESTS_DIR / name
    path.write_text(
        json.dumps([r.to_dict() for r in records], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def load_manifest(name: str = "framing_manifest.json") -> list[FramingRecord]:
    path = config.MANIFESTS_DIR / name
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    records = []
    for item in raw:
        ann = ClipAnnotation(**item.pop("annotation", {}))
        records.append(FramingRecord(annotation=ann, **item))
    return records
