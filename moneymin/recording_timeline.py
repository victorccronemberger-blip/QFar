"""Linha do tempo persistente de gravações, isolada por conta/dispositivo."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import config
from .atomic_io import load_json, save_json
from .device_profile import format_recorded_at

_LOCK = threading.Lock()


@dataclass(frozen=True)
class RecordingSlot:
    email: str
    start_epoch: float
    end_epoch: float

    @property
    def recorded_at(self) -> str:
        return format_recorded_at(self.start_epoch)


def timeline_path() -> Path:
    return config.DATA_DIR / "recording_timeline.json"


def reserve(email: str, duration_s: float, *, now: float | None = None) -> RecordingSlot:
    """Reserva um intervalo sem sobreposição; a primeira gravação começa agora."""
    duration = max(1.0, float(duration_s))
    current = float(time.time() if now is None else now)
    with _LOCK:
        state = load_json(timeline_path(), {"version": 1, "accounts": {}})
        accounts = state.setdefault("accounts", {})
        previous = accounts.get(email) or {}
        previous_end = float(previous.get("last_end_epoch") or 0.0)
        start = max(current, previous_end)
        end = start + duration
        accounts[email] = {
            "last_start_epoch": start,
            "last_end_epoch": end,
            "duration_s": duration,
            "updated_at": current,
        }
        save_json(timeline_path(), state)
    return RecordingSlot(email=email, start_epoch=start, end_epoch=end)


__all__ = ["RecordingSlot", "reserve", "timeline_path"]
