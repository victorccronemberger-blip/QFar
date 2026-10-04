"""Build the portable task index from local metadata, without media downloads."""
from __future__ import annotations

import gzip
import json
import csv
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from moneymin import campaign, ego4d, task_matching

FIELDS = ("clip_uid", "exported_clip_uid", "parent_video_uid", "device", "dur_s",
          "s3_path", "scenario", "scenarios", "window_s", "needs_cut",
          "parent_start_sec", "parent_end_sec", "media_uid", "media_time_offset_s",
          "match_score", "match_confidence", "dedup_clip_uids")


def local_sensor_intervals(path):
    """Continuous ranges of both real sensors; never fill a long data gap."""
    times = [[], []]
    with path.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            for index, names in enumerate((("gyro_x", "gyro_y", "gyro_z"),
                                           ("accl_x", "accl_y", "accl_z"))):
                try:
                    t = float(row["canonical_timestamp_ms"])
                    values = [float(row[name]) for name in names]
                except (KeyError, TypeError, ValueError):
                    continue
                if math.isfinite(t) and all(map(math.isfinite, values)):
                    times[index].append(t)
    ranges = []
    for sensor in times:
        intervals = []
        start = previous = None
        for t in sorted(set(sensor)):
            if previous is None or t - previous > ego4d.IMU_MAX_INTERPOLATION_GAP_MS:
                if previous is not None and previous > start:
                    intervals.append((start / 1000, previous / 1000))
                start = t
            previous = t
        if previous is not None and previous > start:
            intervals.append((start / 1000, previous / 1000))
        ranges.append(intervals)
    both = []
    i = j = 0
    while i < len(ranges[0]) and j < len(ranges[1]):
        a, b = ranges[0][i], ranges[1][j]
        start, end = max(a[0], b[0]), min(a[1], b[1])
        if end > start:
            both.append((start, end))
        if a[1] <= b[1]:
            i += 1
        else:
            j += 1
    return both


def validate_local_sensors(pools):
    """Recheck cached IMU and salvage proven portions around actual gaps."""
    results = {}
    intervals = {}
    accepted = {}
    cut_candidates = {}
    for name, clips in pools.items():
        kept = []
        for clip in clips:
            uid = clip["clip_uid"]
            parent = clip["parent_video_uid"]
            path = ego4d.EGO4D_DIR / (parent + "_imu.csv")
            if not path.exists():
                kept.append(clip)
                continue
            if parent not in intervals:
                intervals[parent] = local_sensor_intervals(path)
            if uid not in results:
                start, end = clip["window_s"]
                results[uid] = any(a <= start and end <= b for a, b in intervals[parent])
                if not results[uid]:
                    # Sample-grid edges can tolerate a few missing milliseconds.
                    # The actual encoder validator decides, not a stricter
                    # approximation of the CSV's first/last timestamp.
                    try:
                        ego4d.build_imu_csv(path, (start, end), validate_only=True)
                        results[uid] = True
                    except RuntimeError:
                        pass
            if results[uid]:
                kept.append(clip)
                continue
            start, end = clip["window_s"]
            for a, b in intervals[parent]:
                a, b = max(a, start), min(b, end)
                if b - a >= 60:
                    cut_candidates.setdefault(name, []).append({**clip, "window_s": (a, b)})
        accepted[name] = kept
    recovered = ego4d.revalidate_task_windows(cut_candidates) if cut_candidates else {}
    verified = {}
    for name, clips in recovered.items():
        verified[name] = []
        for clip in clips:
            uid = clip["clip_uid"]
            if uid not in results:
                try:
                    path = ego4d.EGO4D_DIR / (clip["parent_video_uid"] + "_imu.csv")
                    ego4d.build_imu_csv(path, tuple(clip["window_s"]), validate_only=True)
                    results[uid] = True
                except RuntimeError:
                    results[uid] = False
            if results[uid]:
                verified[name].append(clip)
    print(json.dumps({"local_sensor_windows_checked": len(results),
                      "invalid_local_windows": sum(not ok for ok in results.values()),
                      "salvaged_windows": len({c["clip_uid"] for rows in verified.values() for c in rows})}), flush=True)
    return campaign._union_ranked_clips(
        {name: [*accepted.get(name, ()), *verified.get(name, ())]
         for name in accepted.keys() | verified.keys()}, {})


def main():
    if not ego4d.has_timed_narrations():
        raise SystemExit("Full rebuild requires local timed narrations")
    print("Scanning timed activities (1–30 minutes)", flush=True)
    spans = ego4d.rank_all_task_spans(min_dur_s=60, max_dur_s=1800)
    print(f"Activity windows: {sum(len(rows) for rows in spans.values())}", flush=True)
    official = task_matching.rank_all_tasks(campaign._task_candidates())
    print(f"Compatible official clips: {sum(len(rows) for rows in official.values())}", flush=True)
    evidence = ego4d.narration_evidence_clips(min_dur_s=60, max_dur_s=1800)
    pools = campaign._union_ranked_clips(
        campaign._union_ranked_clips(spans, official), evidence)
    previous = campaign._load_rank_seed() or {}
    known = {name: {clip["clip_uid"] for clip in rows} for name, rows in pools.items()}
    missing = {name: [clip for clip in rows if clip["clip_uid"] not in known.get(name, set())]
               for name, rows in previous.items()}
    recovered = ego4d.revalidate_task_windows(missing)
    pools = campaign._union_ranked_clips(
        {name: [*pools.get(name, ()), *recovered.get(name, ())]
         for name in pools.keys() | recovered.keys()}, {})
    pools = validate_local_sensors(pools)
    pools = campaign._link_rank_history(pools, previous)
    # Rebuild from current evidence: stale seed entries are not approval.
    if not any(pools.values()):
        raise SystemExit("Empty rebuild; existing portable index preserved")
    tasks = {name: [{key: clip[key] for key in FIELDS if key in clip} for clip in clips]
             for name, clips in sorted(pools.items())}
    payload = {"schema": 1, "tasks": tasks}
    output = campaign._rank_seed_path()
    data = gzip.compress(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(), mtime=0)
    temp = output.with_suffix(output.suffix + ".tmp")
    temp.write_bytes(data)
    temp.replace(output)
    # The seed itself is part of the signature. Save the same verified pools
    # against its new hash so the next launch does not repeat the full scan.
    campaign._rank_cache_stamp.cache_clear()
    campaign._load_rank_seed.cache_clear()
    campaign._save_rank_cache(pools)
    by_parent: dict[str, list[tuple[float, float]]] = {}
    per_task = []
    for name, clips in tasks.items():
        if not clips:
            continue
        per_task.append((
            round(sum(float(clip["dur_s"]) for clip in clips) / 3600, 2),
            len(clips), name))
        for clip in clips:
            start, end = clip["window_s"]
            by_parent.setdefault(str(clip["parent_video_uid"]), []).append(
                (float(start), float(end)))
    unique_s = 0.0
    for intervals in by_parent.values():
        intervals.sort()
        merged: list[tuple[float, float]] = []
        for start, end in intervals:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        unique_s += sum(end - start for start, end in merged)
    per_task.sort(reverse=True)
    print(json.dumps({
        "rows": sum(len(c) for c in tasks.values()),
        "unique_clips": len({clip["clip_uid"] for clips in tasks.values() for clip in clips}),
        "parents": len(by_parent),
        "unique_hours": round(unique_s / 3600, 2),
        "clip_hours": round(sum(hours for hours, _count, _name in per_task), 2),
        "filled_tasks": len(per_task),
        "top_tasks": [
            {"task": name, "clip_hours": hours, "clips": count}
            for hours, count, name in per_task[:12]],
        "bytes": len(data)}))


if __name__ == "__main__":
    main()
