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


def journal_flags_valid(row: dict[str, Any]) -> bool:
    """Persisted flags and state labels must keep their declared types."""
    flags = ("finalized", "finalize_requested", "evaluation_required",
             "evaluation_verified", "campaign_reconciled", "register_first",
             "suppress_per_chunk_catbear", "create_attempted", "native_response_schema",
             "remote_fail_attempted", "remote_fail_confirmed", "upload_delete_attempted",
             "upload_delete_confirmed", "session_delete_attempted", "session_delete_confirmed")
    return isinstance(row, dict) and all(
        type(row[key]) is bool for key in flags if key in row) and all(
        isinstance(row[key], str) for key in ("state", "phase") if key in row)


def journal_evaluation_confirmed(row: dict[str, Any]) -> bool:
    """Whether the stored quality gate permits confirmation of a receipt.

    Legacy receipts had neither evaluation flag. Their recorded finalization
    remains usable, without claiming that an evaluation was required or passed.
    An explicit requirement needs an explicit successful verification. A lone
    negative verification has an unknown requirement and cannot prove delivery.
    """
    if not journal_flags_valid(row) or row.get("phase") in {"evaluation_review", "quality_rejected"}:
        return False
    if "evaluation_required" in row:
        return row["evaluation_required"] is False or row.get("evaluation_verified") is True
    if "evaluation_verified" in row:
        return row["evaluation_verified"] is True
    return True


def is_pending_finalization(row: dict[str, Any]) -> bool:
    """A complete chunk can resume evaluation/finalize without resending bytes."""
    upload_id = row.get("upload_id") if isinstance(row, dict) else None
    return (journal_flags_valid(row) and row.get("state") == STATE_DONE
            and row.get("phase") == "done" and row.get("finalize_requested") is True
            and row.get("finalized") is not True
            and isinstance(upload_id, str) and bool(upload_id.strip()))


def journal_delivery_confirmed(row: dict[str, Any]) -> bool:
    """A finalization flag alone cannot identify a confirmed upload receipt.

    Every chunk needs its persisted provider upload ID. Unidentified legacy
    records remain available for review instead of crediting delivery or hiding
    recovery entries. Group ownership, context and completeness are checked by
    the caller.
    """
    upload_id = row.get("upload_id") if isinstance(row, dict) else None
    return (journal_evaluation_confirmed(row)
            and row.get("state") == STATE_DONE and row.get("finalized") is True
            and isinstance(upload_id, str) and bool(upload_id.strip()))


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
        review_required: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.transient = transient
        self.blocked_reason = blocked_reason
        self.phase = phase
        self.review_required = review_required
        self.attempts = None

    @property
    def retryable(self) -> bool:
        """Somente rede, timeout, 408/429 e 5xx merecem nova tentativa."""
        if self.review_required:
            return False
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
    "is_pending_finalization",
    "journal_evaluation_confirmed",
    "journal_flags_valid",
]
