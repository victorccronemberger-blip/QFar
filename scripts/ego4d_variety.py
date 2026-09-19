"""Inventário offline de variedade do catálogo completo, sem filtros de campanha."""
import argparse
import csv
import json
import math
from collections import defaultdict, deque
from pathlib import Path


def diverse_examples(videos, limit=12):
    """Alterna fontes antes de repetir uma; cada exemplo é um vídeo-pai distinto."""
    sources = defaultdict(list)
    for video in videos:
        sources[str(video.get("video_source") or "unknown")].append(video)
    queues = [deque(sorted(rows, key=lambda row: row["video_uid"]))
              for _, rows in sorted(sources.items())]
    result = []
    while queues and len(result) < limit:
        remaining = []
        for queue in queues:
            row = queue.popleft()
            result.append({key: row.get(key) for key in (
                "video_uid", "video_source", "duration_sec", "has_imu", "s3_path")})
            if queue:
                remaining.append(queue)
            if len(result) == limit:
                break
        queues = remaining
    return result


def inventory(directory):
    directory = Path(directory)
    data = json.loads((directory / "ego4d.json").read_text(encoding="utf-8-sig"))
    videos = data["videos"]
    grouped = defaultdict(list)
    local_ids = {path.stem for path in directory.glob("*.mp4")}
    cached_parents = set(local_ids)
    with (directory / "clips.csv").open(encoding="utf-8-sig", newline="") as stream:
        for clip in csv.DictReader(stream):
            if clip.get("exported_clip_uid") in local_ids:
                cached_parents.add(clip.get("parent_video_uid"))
    unique = {row["video_uid"]: row for row in videos}
    for video in unique.values():
        labels = video.get("scenarios") or ["(sem cenário)"]
        for label in set(labels):
            grouped[str(label)].append(video)
    scenarios = []
    for label, rows in sorted(grouped.items()):
        hours = 0.0
        for row in rows:
            duration = float(row.get("duration_sec") or 0)
            if math.isfinite(duration) and duration > 0:
                hours += duration / 3600
        cached = sum(row["video_uid"] in cached_parents for row in rows)
        scenarios.append({
            "scenario": label, "videos": len(rows), "hours": round(hours, 2),
            "with_imu": sum(row.get("has_imu") is True for row in rows),
            "sources": len({row.get("video_source") for row in rows}),
            "parents_with_local_media": cached, "parents_without_local_media": len(rows) - cached,
            "examples": diverse_examples(rows),
        })
    return {"dataset_version": data.get("version"), "unique_videos": len(unique),
            "scenario_count": len(scenarios),
            "scenarios_with_imu": sum(row["with_imu"] > 0 for row in scenarios),
            "notes": ["Vídeos podem ter vários cenários; não some as contagens como vídeos únicos.",
                      "Exemplos são candidatos para exploração, sem aprovação automática para campanha.",
                      "Arquivo local significa presença, não integridade ou cobertura integral do vídeo-pai.",
                      "Nenhum vídeo é excluído por frames isolados ou ausência no cache."],
            "scenarios": scenarios}


def markdown(report):
    lines = ["# Variedade do catálogo Ego4D", "",
             f"{report['unique_videos']} vídeos únicos; {report['scenario_count']} cenários; "
             f"{report['scenarios_with_imu']} cenários com pelo menos um vídeo com IMU.", "",
             *report["notes"], "",
             "| Cenário | Vídeos | Horas | Com IMU | Fontes | Sem mídia local |",
             "|---|---:|---:|---:|---:|---:|"]
    for row in sorted(report["scenarios"], key=lambda row: (-row["videos"], row["scenario"])):
        label = row["scenario"].replace("\n", " ").replace("|", "/")
        lines.append(f"| {label} | {row['videos']} | {row['hours']} | {row['with_imu']} | "
                     f"{row['sources']} | {row['parents_without_local_media']} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = inventory(args.directory)
    args.output.with_suffix(".json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    args.output.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key not in ("scenarios", "notes")}))
