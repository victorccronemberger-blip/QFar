"""Read model for the operation panel; called under the runner lock.

Progress describes the current transfer, not remote receipt or campaign completion.
No tokens, provider responses or arbitrary event payloads enter this model.
"""
from __future__ import annotations

import math


class OperationState:
    def __init__(self, emails=()):
        self.accounts = {email: self._new(email) for email in emails}

    @staticmethod
    def _new(email):
        return dict(email=email, state="queued", progress=None, clip_uid=None,
                    session_id=None, confirmed=0, failed=0, skipped=0)

    def event(self, kind, payload):
        email = payload.get("email")
        if not isinstance(email, str) or not email:
            return
        if kind not in {"account_start", "account_progress", "account_done",
                        "account_retry", "account_excluded", "recording_wait_start"}:
            return
        row = self.accounts.setdefault(email, self._new(email))
        if payload.get("clip_uid"):
            row["clip_uid"] = str(payload["clip_uid"])
        if kind == "account_start":
            row.update(state="preparing", progress=None, session_id=None)
        elif kind == "recording_wait_start":
            row.update(state="waiting", progress=None)
        elif kind == "account_retry":
            row.update(state="recovering", progress=None)
        elif kind == "account_excluded":
            row.update(state="excluded", progress=None)
        elif kind == "account_progress":
            phase = payload.get("phase")
            row["state"] = ("confirming" if phase in {"complete", "finalize", "evaluate"}
                            else "sending" if phase == "transport" else "preparing")
            row["progress"] = None
            if phase == "transport":
                try:
                    percent = float(payload["percent"])
                    if math.isfinite(percent):
                        row["progress"] = max(0, min(100, round(percent)))
                except (KeyError, ValueError, TypeError, OverflowError):
                    pass
        elif kind == "account_done":
            if payload.get("skipped"):
                row["state"] = "skipped"
                row["skipped"] += 1
            elif payload.get("ok") and payload.get("finalized") is True:
                row["state"] = "confirmed"
                row["confirmed"] += 1
            elif payload.get("ok"):
                row["state"] = "unconfirmed"
            else:
                row["state"] = "failed"
                row["failed"] += 1
            row["progress"] = None
            row["session_id"] = str(payload["session_id"]) if payload.get("session_id") else None

    def snapshot(self, terminal=False):
        rows = [dict(row) for row in self.accounts.values()]
        if terminal:
            for row in rows:
                if row["state"] in {"queued", "waiting", "preparing", "sending", "confirming", "recovering"}:
                    row["state"] = "pending"
                    row["progress"] = None
        counts = {}
        for row in rows:
            counts[row["state"]] = counts.get(row["state"], 0) + 1
        return {"accounts": rows, "counts": counts}
