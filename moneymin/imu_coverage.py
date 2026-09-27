"""Select real, continuous sensor windows without filling missing recording."""
from __future__ import annotations

import csv
import hashlib
import math
from functools import lru_cache
from pathlib import Path

from . import config, ego4d
from .atomic_io import load_json, save_json


@lru_cache(maxsize=128)
def _intervals(path: str, size: int, mtime_ns: int, gap_ms: float) -> tuple[tuple[float, float], ...]:
    identity = {"version": 1, "size": size, "mtime_ns": mtime_ns, "gap_ms": gap_ms}
    cache_path = config.DATA_DIR / "imu_coverage" / (hashlib.sha256(path.encode()).hexdigest() + ".json")
    cached = load_json(cache_path, {})
    if isinstance(cached, dict) and cached.get("source") == identity:
        intervals = cached.get("intervals")
        if isinstance(intervals, list) and all(isinstance(row, list) and len(row) == 2
                and all(isinstance(value, (int, float)) and math.isfinite(value) for value in row)
                and row[1] > row[0] for row in intervals):
            return tuple(tuple(row) for row in intervals)
    timestamps = [[], []]
    fields = [("gyro_x", "gyro_y", "gyro_z"), ("accl_x", "accl_y", "accl_z")]
    with open(path, encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"canonical_timestamp_ms", *fields[0], *fields[1]}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("CSV de IMU sem colunas obrigatórias")
        for row in reader:
            try:
                timestamp = float(row["canonical_timestamp_ms"])
            except (ValueError, TypeError):
                continue
            if not math.isfinite(timestamp):
                continue
            for index, names in enumerate(fields):
                try:
                    values = [float(row[name]) for name in names]
                except (ValueError, TypeError):
                    continue
                if all(math.isfinite(value) for value in values):
                    timestamps[index].append(timestamp)
    ranges = []
    for sensor in timestamps:
        ordered = sorted(set(sensor))
        intervals = []
        if ordered:
            start = previous = ordered[0]
            for timestamp in ordered[1:]:
                if timestamp - previous > gap_ms:
                    intervals.append((start, previous))
                    start = timestamp
                previous = timestamp
            intervals.append((start, previous))
        ranges.append(intervals)
    overlap = []
    i = j = 0
    while i < len(ranges[0]) and j < len(ranges[1]):
        a, b = ranges[0][i], ranges[1][j]
        start, end = max(a[0], b[0]), min(a[1], b[1])
        if end > start:
            overlap.append((start / 1000, end / 1000))
        if a[1] <= b[1]:
            i += 1
        else:
            j += 1
    try:
        save_json(cache_path, {"source": identity, "intervals": overlap})
    except OSError:
        pass  # A read-only installation can still validate the original CSV.
    return tuple(overlap)


def refine_candidates(candidates: list[dict], work_dir: Path, min_s: float, max_s: float) -> list[dict]:
    result = []
    seen = {}
    for clip in candidates:
        parent, window = clip.get("parent_video_uid"), clip.get("window_s")
        path = work_dir / f"{parent}_imu.csv"
        if clip.get("source") == "holoassist" or not parent or not window or not path.is_file():
            result.append(clip)
            continue
        try:
            stat = path.stat()
            intervals = _intervals(str(path.resolve()), stat.st_size, stat.st_mtime_ns,
                                   ego4d.IMU_MAX_INTERPOLATION_GAP_MS)
        except (OSError, ValueError, csv.Error):
            # Preserve the normal preparation diagnostic for malformed files.
            result.append(clip)
            continue
        original_start, original_end = map(float, window)
        # Already valid windows retain identity and existing sent history.
        if any(start <= original_start + .002 and end >= original_end - .002 for start, end in intervals):
            result.append(clip)
            continue
        for start, end in intervals:
            start = math.ceil(max(start + .002, original_start) * 1000) / 1000
            end = math.floor(min(end - .002, original_end) * 1000) / 1000
            end = min(end, start + max_s)
            if end - start < min_s:
                continue
            uid = f"{parent}_{start:.3f}_{end:.3f}"
            aliases = {clip["clip_uid"], *clip.get("dedup_clip_uids", [])}
            if uid in seen:
                seen[uid]["dedup_clip_uids"] = sorted(
                    set(seen[uid]["dedup_clip_uids"]) | aliases)
                continue
            # Resolve the media offset anew: an old exported file may cover only
            # the original window and cannot be treated as a full parent video.
            row, video = ego4d.find_clip(uid)
            if row is None or video is None:
                continue
            refined = {**clip, **ego4d._clip_record(row, video), "source": "ego4d",
                       "exported_clip_uid": uid, "parent_start_sec": str(start), "parent_end_sec": str(end),
                       "media_uid": row.get("media_uid", parent),
                       "media_time_offset_s": row.get("media_time_offset_s", 0.0),
                       "imu_refined_from": clip["clip_uid"], "dedup_clip_uids": sorted(aliases)}
            result.append(refined)
            seen[uid] = refined
    return result
