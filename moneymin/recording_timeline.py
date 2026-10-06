"""Linha do tempo persistente de gravações, isolada por conta/dispositivo."""
from __future__ import annotations

import threading
import time
import math
from dataclasses import dataclass
from pathlib import Path

from . import config
from .atomic_io import JsonStateError, load_json_state, save_json
from .device_profile import format_recorded_at
from .campaign_state import campaign_state_operation

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


@campaign_state_operation
def reserve(email: str, duration_s: float, *, now: float | None = None) -> RecordingSlot:
    """Reserva um intervalo sem sobreposição; a primeira gravação começa agora."""
    duration = max(1.0, float(duration_s))
    current = float(time.time() if now is None else now)
    with _LOCK:
        state = load_json_state(timeline_path(), {"version": 1, "accounts": {}})
        # Every account is checked before allocating or replacing the document.
        # Historical finite numeric strings keep their previous conversion;
        # an invalid prior reservation cannot become a fresh empty timeline.
        try:
            existing_accounts = state.get("accounts", {})
            if not isinstance(existing_accounts, dict):
                raise ValueError
            for previous in existing_accounts.values():
                if not isinstance(previous, dict):
                    raise ValueError
                for key in ("last_start_epoch", "last_end_epoch", "duration_s", "updated_at"):
                    if key in previous and (isinstance(previous[key], bool)
                            or not math.isfinite(float(previous[key]))):
                        raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise JsonStateError("Linha do tempo local inválida; o arquivo foi preservado. Restaure um backup válido antes de reservar outro intervalo.") from None
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
