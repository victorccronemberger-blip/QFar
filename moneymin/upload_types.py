"""Estados, erros e resultados do protocolo de upload Minute."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

STATE_CREATING = "creating"
STATE_TRANSPORT = "transport"
STATE_COMPLETING = "completing"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_RETRY_LATE = "retry_late"
STATE_LOSS = "loss"
STATE_QUARANTINE = "quarantine"

TRANSIENT_STATES = {
    STATE_CREATING,
    STATE_TRANSPORT,
    STATE_COMPLETING,
    STATE_RETRY_LATE,
}


class UploadError(RuntimeError):
    """Falha em uma etapa do upload ou da finalização da sessão."""

    attempts: int | None

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        transient: bool | None = None,
        blocked_reason: str | None = None,
        phase: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.transient = transient
        self.blocked_reason = blocked_reason
        self.phase = phase
        self.attempts = None

    @property
    def retryable(self) -> bool:
        """Somente rede, timeout, 408/429 e 5xx merecem nova tentativa."""
        if self.transient is not None:
            return self.transient
        if self.status_code is None:
            return True
        return self.status_code in (408, 429) or self.status_code >= 500


@dataclass
class ChunkResult:
    """Resultado de um chunk individual dentro de uma sessão."""

    upload_id: str
    chunk_index: int
    log_id: str
    blob_path: str
    size_bytes: int
    duration_ms: int
    raw_create: dict[str, Any] = field(default_factory=dict)
    raw_complete: dict[str, Any] = field(default_factory=dict)
    evaluate_result: dict[str, Any] | None = None
    state: str = STATE_DONE
    attempts: int = 1
    error: str | None = None
    sidecar_blob_path: str = ""
    sidecar_size_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "upload_id": self.upload_id,
            "chunk_index": self.chunk_index,
            "log_id": self.log_id,
            "blob_path": self.blob_path,
            "size_bytes": self.size_bytes,
            "duration_ms": self.duration_ms,
            "raw_create": self.raw_create,
            "raw_complete": self.raw_complete,
            "evaluate_result": self.evaluate_result,
            "state": self.state,
            "attempts": self.attempts,
            "error": self.error,
            "sidecar_blob_path": self.sidecar_blob_path,
            "sidecar_size_bytes": self.sidecar_size_bytes,
        }


@dataclass
class UploadResult:
    """Resultado de uma sessão de upload completa (um ou mais chunks)."""

    session_id: str
    org_key: str
    task_id: str | None
    chunks: list[ChunkResult] = field(default_factory=list)
    finalized: bool = False
    finalize_status: int | None = None
    total_size_bytes: int = 0
    total_duration_ms: int = 0
    recorded_at: str = ""

    @property
    def upload_id(self) -> str:
        return self.chunks[0].upload_id if self.chunks else ""

    @property
    def blob_path(self) -> str:
        return self.chunks[0].blob_path if self.chunks else ""

    @property
    def log_id(self) -> str:
        return self.chunks[0].log_id if self.chunks else ""

    @property
    def size_bytes(self) -> int:
        return self.total_size_bytes

    @property
    def duration_ms(self) -> int:
        return self.total_duration_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "org_key": self.org_key,
            "task_id": self.task_id,
            "finalized": self.finalized,
            "finalize_status": self.finalize_status,
            "total_size_bytes": self.total_size_bytes,
            "total_duration_ms": self.total_duration_ms,
            "recorded_at": self.recorded_at,
            "chunks": [chunk.to_dict() for chunk in self.chunks],
        }


__all__ = [
    "ChunkResult",
    "STATE_COMPLETING",
    "STATE_CREATING",
    "STATE_DONE",
    "STATE_FAILED",
    "STATE_LOSS",
    "STATE_QUARANTINE",
    "STATE_RETRY_LATE",
    "STATE_TRANSPORT",
    "TRANSIENT_STATES",
    "UploadError",
    "UploadResult",
]
