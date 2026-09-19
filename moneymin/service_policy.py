"""Contratos operacionais do serviço, sem identidade ou telemetria sintética."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class RecordingPolicy:
    version: int
    min_duration_ms: int
    max_duration_ms: int
    backlog_cap_ms: int

    @classmethod
    def parse(cls, payload: Any) -> "RecordingPolicy":
        if not isinstance(payload, dict):
            raise ValueError("Configuração de gravação não é um objeto.")
        keys = ("configVersion", "minDurationMs", "maxDurationMs", "backlogCapMs")
        if any(type(payload.get(key)) is not int for key in keys):
            raise ValueError("Configuração de gravação incompleta ou com tipos inválidos.")
        version, minimum, maximum, backlog = (payload[key] for key in keys)
        if version < 1 or minimum <= 0 or maximum < minimum or backlog <= 0:
            raise ValueError("Configuração de gravação possui limites inválidos.")
        return cls(version, minimum, maximum, backlog)

    def limits(self) -> dict[str, int]:
        return {"min_duration_ms": self.min_duration_ms,
                "max_duration_ms": self.max_duration_ms,
                "backlog_cap_ms": self.backlog_cap_ms}

    def validate_duration(self, duration: Any) -> None:
        if type(duration) is not int or not self.min_duration_ms <= duration <= self.max_duration_ms:
            raise ValueError("Duração fora dos limites de gravação desta sessão.")

    def validate_recording_time(self, recorded_at: Any, duration_ms: int, now: float) -> None:
        try:
            if not isinstance(recorded_at, str):
                raise ValueError()
            start = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
            if start.tzinfo is None:
                raise ValueError()
            start_ms = start.timestamp() * 1000
        except (ValueError, OverflowError, OSError) as exc:
            raise ValueError("Horário de gravação inválido ou sem fuso horário.") from exc
        now_ms = now * 1000
        if start_ms < now_ms - self.backlog_cap_ms or start_ms + duration_ms > now_ms:
            raise ValueError("Horário de gravação fora da janela permitida nesta sessão.")
