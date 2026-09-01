"""Gera o índice Ego4D portátil e mínimo usado por instalações novas.

O cache de origem é produzido localmente a partir das anotações licenciadas.
O artefato distribuído remove integralmente texto de narração e mantém apenas
os IDs/janelas necessários para selecionar e baixar mídia sob demanda.
"""
from __future__ import annotations

import gzip
import json
import pickle
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "data" / "task_rank_cache.pkl"
OUTPUT = ROOT / "moneymin" / "resources" / "ego4d_task_rank_seed.json.gz"
FIELDS = (
    "clip_uid",
    "device",
    "dur_s",
    "exported_clip_uid",
    "match_confidence",
    "match_score",
    "media_time_offset_s",
    "media_uid",
    "needs_cut",
    "parent_end_sec",
    "parent_start_sec",
    "parent_video_uid",
    "s3_path",
    "scenario",
    "scenarios",
    "window_s",
)


def main() -> None:
    _stamp, buckets = pickle.loads(SOURCE.read_bytes())
    tasks: dict[str, list[dict]] = {}
    for name, items in buckets.items():
        tasks[str(name)] = [
            {field: item[field] for field in FIELDS if field in item}
            for item in items
        ]
    payload = {
        "schema": 1,
        "source": "Ego4D v2 licensed metadata; media is not embedded",
        "tasks": tasks,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(gzip.compress(encoded, compresslevel=9, mtime=0))
    clips = sum(len(items) for items in tasks.values())
    print(
        f"{OUTPUT}: {len(tasks)} tarefas, {clips} trechos, "
        f"{OUTPUT.stat().st_size} bytes"
    )


if __name__ == "__main__":
    main()
