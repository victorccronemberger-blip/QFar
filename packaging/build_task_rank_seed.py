"""Gera o índice Ego4D portátil e mínimo usado por instalações novas.

O cache de origem é produzido localmente a partir das anotações licenciadas.
O artefato distribuído remove integralmente texto de narração e mantém apenas
os IDs/janelas necessários para selecionar e baixar mídia sob demanda.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

from moneymin import ego4d


ROOT = Path(__file__).resolve().parent.parent
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
    buckets = ego4d.rank_all_task_spans()
    long_buckets = ego4d.rank_all_task_spans(
        min_dur_s=600, max_dur_s=1800)
    for name, items in long_buckets.items():
        buckets.setdefault(name, []).extend(items)
    tasks: dict[str, list[dict]] = {}
    for name, items in sorted(buckets.items()):
        portable: list[dict] = []
        seen: set[str] = set()
        for item in items:
            identity = str(item.get("clip_uid") or "")
            if not identity or identity in seen:
                continue
            seen.add(identity)
            portable.append({
                field: item[field] for field in FIELDS if field in item
            })
        tasks[str(name)] = portable
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
    long_clips = sum(
        float(item.get("dur_s") or 0) >= 600
        for items in tasks.values()
        for item in items
    )
    print(
        f"{OUTPUT}: {len(tasks)} tarefas, {clips} trechos, "
        f"{long_clips} longos, {OUTPUT.stat().st_size} bytes"
    )


if __name__ == "__main__":
    main()
