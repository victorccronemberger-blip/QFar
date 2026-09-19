"""Auditoria offline do catálogo Ego4D. Não baixa arquivos nem envia conteúdo."""
import argparse
import csv
import json
import math
from pathlib import Path


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def audit(directory):
    directory = Path(directory)
    metadata = json.loads((directory / "ego4d.json").read_text(encoding="utf-8-sig"))
    videos = metadata.get("videos") if isinstance(metadata, dict) else None
    if not isinstance(videos, list) or any(not isinstance(video, dict) for video in videos):
        raise ValueError("Catálogo de vídeos inválido")
    by_id = {video.get("video_uid"): video for video in videos if isinstance(video.get("video_uid"), str)}
    issues = {}
    examples = {}

    def issue(name, uid):
        issues[name] = issues.get(name, 0) + 1
        examples.setdefault(name, [])
        if len(examples[name]) < 5:
            examples[name].append(str(uid or "missing"))

    seen = set()
    count = 0
    with (directory / "clips.csv").open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"exported_clip_uid", "parent_video_uid", "parent_start_sec", "parent_end_sec", "s3_path"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("Manifesto sem campos obrigatórios")
        for clip in reader:
            count += 1
            uid = clip.get("exported_clip_uid")
            if not uid:
                issue("missing_clip_id", uid)
            elif uid in seen:
                issue("duplicate_clip_id", uid)
            seen.add(uid)
            parent = by_id.get(clip.get("parent_video_uid"))
            if parent is None:
                issue("missing_parent", uid)
            start, end = number(clip.get("parent_start_sec")), number(clip.get("parent_end_sec"))
            if start is None or end is None or start < 0 or end <= start:
                issue("invalid_time_window", uid)
            elif parent is not None:
                duration = number(parent.get("duration_sec"))
                if duration is not None and end > duration + 0.1:
                    issue("window_exceeds_parent", uid)
            source = clip.get("s3_path") or ""
            bucket, _, key = source.removeprefix("s3://").partition("/")
            if not source.startswith("s3://") or not bucket or not key:
                issue("invalid_source", uid)
    return {
        "offline": True, "dataset_version": metadata.get("version"),
        "videos": len(videos), "unique_video_ids": len(by_id), "clips": count,
        "videos_with_imu_flag": sum(video.get("has_imu") is True for video in videos),
        "issues": issues, "examples": examples,
        "limits": ["Metadata checks do not verify video content, full decoding, IMU continuity or remote availability.",
                   "Scenario labels and narrations are evidence, not proof of continuous activity."],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    report = json.dumps(audit(arguments.directory), indent=2, ensure_ascii=False)
    if arguments.output:
        arguments.output.write_text(report + "\n", encoding="utf-8")
    print(report)
