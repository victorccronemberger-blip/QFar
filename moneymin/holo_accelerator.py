"""Pré-aquecimento retomável do subconjunto útil do HoloAssist."""
from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import config, holoassist
from .atomic_io import load_json, save_json

DEFAULT_TASK = "Furniture Assembly"
SENSOR_NAMES = (
    "Accelerometer_sync.txt",
    "Gyroscope_sync.txt",
    "Magnetometer_sync.txt",
)


def state_path() -> Path:
    return holoassist.data_dir() / "warm_state.json"


def stop_path() -> Path:
    return holoassist.data_dir() / "warm.stop"


def native_path(clip: dict[str, Any], work_dir: Path | None = None) -> Path:
    video_name = str(clip["video_name"])
    safe_stem = "holoassist_" + video_name.replace("/", "_").replace("\\", "_")
    return Path(work_dir or config.MEDIA_DATA_DIR / "ego4d") / f"{safe_stem}_native.mp4"


def source_path(clip: dict[str, Any]) -> Path:
    folder = holoassist.data_dir() / "recordings" / str(clip["video_name"])
    pitchshift = folder / "Video_pitchshift.mp4"
    compressed = folder / "Video_compress.mp4"
    return compressed if compressed.exists() and not pitchshift.exists() else pitchshift


def sensors_ready(clip: dict[str, Any]) -> bool:
    folder = (
        holoassist.data_dir() / "recordings" / str(clip["video_name"]) / "IMU"
    )
    return all((folder / name).is_file() and (folder / name).stat().st_size > 0
               for name in SENSOR_NAMES)


def clip_ready(clip: dict[str, Any], work_dir: Path | None = None) -> bool:
    source = source_path(clip)
    native = native_path(clip, work_dir)
    return (
        source.is_file() and source.stat().st_size > 1024 * 1024
        and native.is_file() and native.stat().st_size > 1024 * 1024
        and sensors_ready(clip)
    )


def eligible_clips(
    task: str = DEFAULT_TASK,
    *,
    min_dur_s: float = 60,
    max_dur_s: float = 1800,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    clips = holoassist.list_clips(task, min_dur_s=min_dur_s, max_dur_s=max_dur_s)
    return clips[:max(0, int(limit))] if limit is not None else clips


def cache_status(
    task: str = DEFAULT_TASK,
    *,
    min_dur_s: float = 60,
    max_dur_s: float = 1800,
    limit: int | None = None,
    work_dir: Path | None = None,
) -> dict[str, Any]:
    clips = eligible_clips(
        task, min_dur_s=min_dur_s, max_dur_s=max_dur_s, limit=limit)
    ready = sum(clip_ready(clip, work_dir) for clip in clips)
    partial = sum(
        not clip_ready(clip, work_dir)
        and (source_path(clip).exists() or native_path(clip, work_dir).exists())
        for clip in clips
    )
    previous = load_json(state_path(), {})
    return {
        "task": task,
        "total": len(clips),
        "ready": ready,
        "partial": partial,
        "pending": len(clips) - ready,
        "last_run": previous,
    }


def request_stop() -> Path:
    path = stop_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("stop\n", encoding="utf-8")
    return path


def warm_cache(
    task: str = DEFAULT_TASK,
    *,
    min_dur_s: float = 60,
    max_dur_s: float = 1800,
    limit: int | None = None,
    min_free_gb: float = 150.0,
    work_dir: Path | None = None,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Baixa e normaliza clips elegíveis; cache pronto é pulado com segurança."""
    # Import tardio evita ciclo: campaign importa holoassist.
    from .campaign import prepare_holoassist_clip

    work = Path(work_dir or config.MEDIA_DATA_DIR / "ego4d")
    work.mkdir(parents=True, exist_ok=True)
    clips = eligible_clips(
        task, min_dur_s=min_dur_s, max_dur_s=max_dur_s, limit=limit)
    stop_path().unlink(missing_ok=True)
    started = time.time()
    state: dict[str, Any] = {
        "status": "running",
        "pid": os.getpid(),
        "task": task,
        "total": len(clips),
        "ready": 0,
        "skipped": 0,
        "failed": 0,
        "current": None,
        "started_at": started,
        "updated_at": started,
        "errors": [],
    }

    def emit(kind: str, **payload: Any) -> None:
        if progress:
            progress(kind, payload)

    def persist() -> None:
        state["updated_at"] = time.time()
        save_json(state_path(), state)

    persist()
    try:
        for index, clip in enumerate(clips, 1):
            if stop_path().exists():
                state["status"] = "stopped"
                break
            name = str(clip["video_name"])
            state["current"] = name
            state["index"] = index
            persist()
            if clip_ready(clip, work):
                state["ready"] += 1
                state["skipped"] += 1
                emit("cached", index=index, total=len(clips), video_name=name)
                persist()
                continue
            free_gb = shutil.disk_usage(work).free / 1024 ** 3
            if free_gb < min_free_gb:
                state["status"] = "disk_limit"
                state["free_gb"] = round(free_gb, 2)
                break
            emit("start", index=index, total=len(clips), video_name=name,
                 free_gb=free_gb)
            try:
                def preparation_progress(
                    phase: str,
                    data: dict[str, Any],
                    *,
                    current_index: int = index,
                    current_name: str = name,
                ) -> None:
                    phase_data = dict(data)
                    phase_current = phase_data.pop("current", None)
                    phase_total = phase_data.pop("total", None)
                    emit(
                        "phase", index=current_index, total=len(clips),
                        video_name=current_name, phase=phase,
                        phase_current=phase_current, phase_total=phase_total,
                        **phase_data)

                prepare_holoassist_clip(
                    clip, work,
                    progress=preparation_progress,
                )
            except Exception as exc:  # noqa: BLE001 — registra e avança
                state["failed"] += 1
                state["errors"] = [
                    *state["errors"][-49:],
                    {"video_name": name, "error": f"{type(exc).__name__}: {exc}"},
                ]
                emit("failed", index=index, total=len(clips), video_name=name,
                     error=str(exc))
            else:
                state["ready"] += 1
                emit("done", index=index, total=len(clips), video_name=name)
            persist()
        else:
            state["status"] = "complete"
    except KeyboardInterrupt:
        state["status"] = "stopped"
    finally:
        state["current"] = None
        state["elapsed_s"] = round(time.time() - started, 1)
        persist()
    return state


__all__ = [
    "DEFAULT_TASK",
    "cache_status",
    "clip_ready",
    "eligible_clips",
    "native_path",
    "request_stop",
    "source_path",
    "state_path",
    "warm_cache",
]
