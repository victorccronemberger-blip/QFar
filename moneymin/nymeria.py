"""NymeriaPlus library index + window selection for Minute campaigns.

Philosophy: do **not** artificially throttle NymeriaPlus. The only hard caps are
Minute recording-config bounds (min/max duration) and real IMU/RGB coverage.
When Nymeria can fill a window with measured Aria IMU+RGB, use the full span.
"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any

from . import nymeria_vrs

DEFAULT_ROOT = Path(
    os.environ.get(
        "NYMERIA_ROOT",
        str(Path.home() / "OneDrive" / "Desktop" / "NymeriaPlus"),
    )
)


def data_root() -> Path:
    return Path(os.environ.get("NYMERIA_ROOT", DEFAULT_ROOT)).expanduser().resolve()


def sequence_dirs(root: Path | None = None) -> list[Path]:
    base = Path(root or data_root())
    if not base.is_dir():
        return []
    out: list[Path] = []
    for child in sorted(base.iterdir()):
        if child.is_dir() and (child / "metadata.json").is_file():
            out.append(child)
    return out


def load_metadata(seq_dir: Path) -> dict[str, Any]:
    path = Path(seq_dir) / "metadata.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _read_narration_rows(seq_dir: Path) -> list[dict[str, str]]:
    narr = Path(seq_dir) / "narration"
    rows: list[dict[str, str]] = []
    for name in ("atomic_action.csv", "activity_summarization.csv",
                 "motion_narration.csv"):
        path = narr / name
        if not path.is_file():
            continue
        with path.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                rows.append({k: (v or "") for k, v in row.items()})
    return rows


def measured_span_s(seq_dir: Path) -> float:
    """RGB∩IMU DEVICE_TIME span in seconds (full usable length)."""
    t0, t1 = device_window_for_sequence(seq_dir)
    return max(0.0, (t1 - t0) / 1e9)


def list_sequences(root: Path | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for seq_dir in sequence_dirs(root):
        try:
            meta = load_metadata(seq_dir)
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
        uid = str(meta.get("uid") or seq_dir.name)
        motion = seq_dir / "recording_head" / "data" / "motion.vrs"
        data = seq_dir / "recording_head" / "data" / "data.vrs"
        # Prefer measured VRS span over metadata action_duration (often shorter).
        duration = 0.0
        if motion.is_file() and data.is_file():
            try:
                duration = measured_span_s(seq_dir)
            except Exception:
                duration = 0.0
        if duration <= 0:
            duration = float(
                meta.get("head_duration_sec")
                or meta.get("action_duration_sec")
                or 0.0
            )
        items.append({
            "seq_id": seq_dir.name,
            "uid": uid,
            "path": str(seq_dir),
            "script": str(meta.get("script") or ""),
            "location": str(meta.get("location") or ""),
            "duration_s": duration,
            "has_motion_vrs": motion.is_file(),
            "has_data_vrs": data.is_file(),
            "metadata": meta,
        })
    return items


def device_window_for_sequence(
    seq_dir: Path,
    *,
    start_s: float = 0.0,
    end_s: float | None = None,
) -> tuple[int, int]:
    """Map relative seconds [0, duration] onto DEVICE_TIME of head RGB/IMU."""
    data_vrs = Path(seq_dir) / "recording_head" / "data" / "data.vrs"
    motion_vrs = Path(seq_dir) / "recording_head" / "data" / "motion.vrs"
    if not data_vrs.is_file():
        raise FileNotFoundError(f"data.vrs ausente em {seq_dir}")
    if not motion_vrs.is_file():
        raise FileNotFoundError(f"motion.vrs ausente em {seq_dir}")
    rgb0, rgb1 = nymeria_vrs.rgb_stream_span_ns(data_vrs)
    imu0, imu1 = nymeria_vrs.imu_stream_span_ns(motion_vrs)
    t0 = max(rgb0, imu0)
    t1 = min(rgb1, imu1)
    if t1 <= t0:
        raise RuntimeError("sem overlap DEVICE_TIME entre RGB e IMU Nymeria")
    span_s = (t1 - t0) / 1e9
    end = span_s if end_s is None else float(end_s)
    start = max(0.0, float(start_s))
    end = min(span_s, max(start + 1.0, end))
    return int(t0 + start * 1e9), int(t0 + end * 1e9)


def _window_record(
    seq: dict[str, Any], start: float, end: float,
) -> dict[str, Any]:
    return {
        "clip_uid": f"nymeria:{seq['seq_id']}:{start:.3f}:{end:.3f}",
        "seq_id": seq["seq_id"],
        "uid": seq.get("uid"),
        "path": seq["path"],
        "window_s": (start, end),
        "dur_s": end - start,
        "script": seq.get("script") or "",
        "scenario": seq.get("script") or "",
        "source": "nymeria",
        "needs_cut": True,
        "parent_video_uid": seq["seq_id"],
        "exported_clip_uid": f"nymeria:{seq['seq_id']}:{start:.3f}:{end:.3f}",
    }


def list_windows(
    seq: dict[str, Any],
    *,
    min_dur_s: float = 60.0,
    max_dur_s: float = 1800.0,
) -> list[dict[str, Any]]:
    """Full utilization of RGB∩IMU span under Minute min/max duration only.

    - Uses measured VRS span (not capped to action_duration_sec).
    - Emits the longest legal window first, then non-overlapping tiles of
      ``max_dur_s`` so long sequences are fully harvestable.
    - No artificial stride thinning: only Minute bounds + real coverage.
    """
    duration = float(seq.get("duration_s") or 0.0)
    path = Path(seq["path"])
    if seq.get("has_motion_vrs") and seq.get("has_data_vrs"):
        try:
            duration = max(duration, measured_span_s(path))
        except Exception:
            pass
    if duration < min_dur_s:
        return []

    windows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(start: float, end: float) -> None:
        if end - start < min_dur_s - 1e-6:
            return
        end = min(duration, start + max_dur_s, end)
        if end - start < min_dur_s - 1e-6:
            return
        row = _window_record(seq, start, end)
        if row["clip_uid"] in seen:
            return
        seen.add(row["clip_uid"])
        windows.append(row)

    # 1) Longest single legal take from t=0
    _add(0.0, min(duration, max_dur_s))

    # 2) Non-overlapping tiles covering the rest of the sequence
    start = 0.0
    while start + min_dur_s <= duration + 1e-6:
        end = min(duration, start + max_dur_s)
        _add(start, end)
        if end >= duration - 1e-6:
            break
        start = end

    # Longest first for campaign preference
    windows.sort(key=lambda w: -float(w["dur_s"]))
    return windows


def automatic_candidates(
    *,
    min_dur_s: float = 60.0,
    max_dur_s: float = 1800.0,
    root: Path | None = None,
    max_results: int | None = None,
) -> list[dict[str, Any]]:
    """All eligible Nymeria windows. ``max_results`` only truncates after full build."""
    out: list[dict[str, Any]] = []
    for seq in list_sequences(root):
        if not (seq.get("has_motion_vrs") and seq.get("has_data_vrs")):
            continue
        out.extend(list_windows(seq, min_dur_s=min_dur_s, max_dur_s=max_dur_s))
    # Prefer longer takes (Minute-friendly single uploads)
    out.sort(key=lambda c: -float(c.get("dur_s") or 0))
    if max_results is not None:
        return out[: max(0, int(max_results))]
    return out
