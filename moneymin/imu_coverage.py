"""Select real, continuous sensor windows without filling missing recording."""
from __future__ import annotations

import csv
import hashlib
import io
import math
from functools import lru_cache
from pathlib import Path

from . import config, ego4d
from .atomic_io import load_json, save_json


class _HashedReader(io.RawIOBase):
    """Hash the exact binary bytes consumed by the canonical text scanner."""

    def __init__(self, stream, digest):
        super().__init__()
        self.stream, self.digest, self.bytes_read = stream, digest, 0

    def readable(self):
        return True

    def readinto(self, buffer):
        count = self.stream.readinto(buffer)
        if count:
            self.digest.update(memoryview(buffer)[:count])
            self.bytes_read += count
        return count


def _source_fingerprint(path: str, size: int, mtime_ns: int) -> str:
    """Stream source identity before looking up any memoized interval result."""
    expected = (size, mtime_ns)
    before = Path(path).stat()
    if (before.st_size, before.st_mtime_ns) != expected:
        raise ValueError("Fonte de IMU alterada durante a leitura.")
    digest, count = hashlib.sha256(), 0
    with open(path, "rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
            count += len(block)
    after = Path(path).stat()
    if count != size or (after.st_size, after.st_mtime_ns) != expected:
        raise ValueError("Fonte de IMU alterada durante a leitura.")
    return digest.hexdigest()


@lru_cache(maxsize=128)
def _canonical_intervals(path: str, source_sha256: str, gap_ms: float) -> tuple[tuple[float, float], ...]:
    """Memoize only source-computed continuity, never unverified cache records."""
    timestamps = [[], []]
    fields = [("gyro_x", "gyro_y", "gyro_z"), ("accl_x", "accl_y", "accl_z")]
    before = Path(path).stat()
    digest = hashlib.sha256()
    with open(path, "rb") as binary:
        hashed = _HashedReader(binary, digest)
        with io.TextIOWrapper(io.BufferedReader(hashed), encoding="utf-8", newline="") as stream:
            timestamps = _scan_timestamps(stream, fields)
    after = Path(path).stat()
    if (digest.hexdigest() != source_sha256 or hashed.bytes_read != before.st_size
            or (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns)):
        raise ValueError("Fonte de IMU alterada durante a leitura.")
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
    return tuple(overlap)


def _scan_timestamps(stream, fields):
    """Preserve the original CSV scanner, including both sensors and unordered input."""
    timestamps = [[], []]
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
    return timestamps


@lru_cache(maxsize=128)
def _intervals(path: str, size: int, mtime_ns: int, gap_ms: float,
               source_sha256: str) -> tuple[tuple[float, float], ...]:
    identity = {"version": 2, "size": size, "mtime_ns": mtime_ns, "gap_ms": gap_ms,
                "sha256": source_sha256}
    canonical = _canonical_intervals(path, source_sha256, gap_ms)
    cache_path = config.DATA_DIR / "imu_coverage" / (hashlib.sha256(path.encode()).hexdigest() + ".json")
    cached = load_json(cache_path, {})
    if isinstance(cached, dict) and cached.get("source") == identity:
        intervals = cached.get("intervals")
        if (isinstance(intervals, list) and all(isinstance(row, list) and len(row) == 2
                and all(type(value) in (int, float) and math.isfinite(value) for value in row)
                and row[1] > row[0] for row in intervals)
                and tuple(tuple(row) for row in intervals) == canonical):
            return canonical
    try:
        save_json(cache_path, {"source": identity, "intervals": canonical})
    except OSError:
        pass  # A read-only installation can still validate the original CSV.
    return canonical


def continuous_intervals_for_imu(
    imu_path: Path, *, gap_ms: float | None = None,
) -> tuple[tuple[float, float], ...]:
    """Gyro∩accel continuity intervals (seconds) for a local Ego4D IMU CSV."""
    path = Path(imu_path)
    if not path.is_file():
        return ()
    gap = float(ego4d.IMU_MAX_INTERPOLATION_GAP_MS if gap_ms is None else gap_ms)
    try:
        stat = path.stat()
        source_path = str(path.resolve())
        fingerprint = _source_fingerprint(source_path, stat.st_size, stat.st_mtime_ns)
        return _intervals(source_path, stat.st_size, stat.st_mtime_ns, gap, fingerprint)
    except (OSError, ValueError, csv.Error):
        return ()


def _evidenced_subwindows(clip: dict, candidate: dict, min_s: float, max_s: float) -> list[dict]:
    """Restrict a sensor cut to current task proof before replacing its carrier."""
    if clip.get("selection_evidence") is None:
        return [candidate]
    try:
        previous = ego4d.revalidate_selection_evidence(clip, fresh=False)
        task = previous["task"]
        name = task["name"]
        start, end = map(float, candidate["window_s"])
        checked = ego4d.revalidate_task_windows(
            {name: [candidate]}, min_dur_s=min_s, max_dur_s=max_s).get(name, ())
    except (OSError, ValueError, TypeError, KeyError):
        return []
    result = []
    for record in checked:
        try:
            proof_start, proof_end = map(float, record["window_s"])
            if (proof_start < start or proof_end > end
                    or record.get("parent_video_uid") != candidate.get("parent_video_uid")
                    or not min_s <= proof_end - proof_start <= max_s):
                continue
            proved = {**candidate, **record}
            proved.pop("selection_evidence", None)
            proved = ego4d.attach_selection_evidence(
                proved, name, task_id=task.get("id"), registry_key=task.get("registry_key"))
            proved["selection_evidence"] = ego4d.revalidate_selection_evidence(
                proved, task_name=name, task_id=task.get("id"), registry_key=task.get("registry_key"),
                fresh=False)
            result.append(proved)
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return result


@ego4d.selection_boundary
def carve_continuous_window(
    clip: dict, work_dir: Path, *, min_s: float, max_s: float,
) -> dict | None:
    """Return a refined Ego4D clip inside the longest continuous IMU overlap.

    Does not invent sensor data. Returns None when no ≥min_s continuous piece
    remains inside the original window.
    """
    if clip.get("source") == "holoassist":
        return None
    parent = clip.get("parent_video_uid")
    window = clip.get("window_s")
    if not parent or not window:
        return None
    path = Path(work_dir) / f"{parent}_imu.csv"
    intervals = continuous_intervals_for_imu(path)
    if not intervals:
        return None
    original_start, original_end = map(float, window)
    # Prefer the longest overlap with the requested window.
    candidates = []
    for start, end in intervals:
        start = math.ceil(max(start + 0.002, original_start) * 1000) / 1000
        end = math.floor(min(end - 0.002, original_end) * 1000) / 1000
        end = min(end, start + max_s)
        if end - start >= min_s:
            candidates.append((end - start, start, end))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    aliases = {clip.get("clip_uid"), *(clip.get("dedup_clip_uids") or [])}
    aliases.discard(None)
    proved = []
    for _, start, end in candidates:
        uid = f"{parent}_{start:.3f}_{end:.3f}"
        # Prefer catalog resolution when the local index is already warm.
        row = video = None
        try:
            row, video = ego4d.find_clip(uid)
        except Exception:  # noqa: BLE001 — offline / missing AWS profile
            row = video = None
        candidate = {
            **clip, "clip_uid": uid, "exported_clip_uid": uid,
            "window_s": (start, end), "dur_s": end - start, "needs_cut": True,
            "parent_start_sec": str(start), "parent_end_sec": str(end),
            "imu_carved_from": clip.get("clip_uid"),
            "dedup_clip_uids": sorted(str(a) for a in aliases),
        }
        if row is not None and video is not None:
            candidate.update(ego4d._clip_record(row, video))
        proved.extend(_evidenced_subwindows(clip, candidate, min_s, max_s))
        if clip.get("selection_evidence") is None:
            break
    return max(proved, key=lambda row: row["window_s"][1] - row["window_s"][0]) if proved else None


@ego4d.selection_boundary
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
            source_path = str(path.resolve())
            fingerprint = _source_fingerprint(source_path, stat.st_size, stat.st_mtime_ns)
            intervals = _intervals(source_path, stat.st_size, stat.st_mtime_ns,
                                   ego4d.IMU_MAX_INTERPOLATION_GAP_MS, fingerprint)
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
            for proved in _evidenced_subwindows(clip, refined, min_s, max_s):
                proved_uid = proved["clip_uid"]
                if proved_uid in seen:
                    seen[proved_uid]["dedup_clip_uids"] = sorted(
                        set(seen[proved_uid]["dedup_clip_uids"]) | aliases)
                    continue
                result.append(proved)
                seen[proved_uid] = proved
    return result
