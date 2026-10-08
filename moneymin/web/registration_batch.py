"""Strict public registration progress; ambiguous stored state is preserved."""
from __future__ import annotations

import re
from pathlib import Path
from .. import credential_store
from ..atomic_io import JsonStateError, load_json_state, save_json
from .registration_state import validate_steps

_FIELDS = {"state", "total", "completed", "created", "failed", "results", "current_email",
           "current_step", "domain", "stopped", "error", "request_id", "request_fingerprint"}
_RESULT_FIELDS = {"email", "nome", "sobrenome", "gender", "birth_month", "birth_year",
                  "created", "partial", "removable", "removed", "error", "steps"}


def _invalid():
    raise JsonStateError("O progresso dos cadastros está inválido; preserve o arquivo antes de continuar.")


def validate(state) -> dict:
    """Check a persisted/public snapshot without exposing any invalid payload."""
    if (type(state) is not dict or set(state) - _FIELDS
            or not isinstance(state.get("state"), str)
            or state["state"] not in {"idle", "running", "stopping", "done", "failed"}):
        _invalid()
    for key in ("total", "completed", "created", "failed"):
        if key in state and (type(state[key]) is not int or state[key] < 0):
            _invalid()
    for key in ("current_email", "current_step", "domain", "request_id", "request_fingerprint"):
        if key in state and not isinstance(state[key], str):
            _invalid()
    if ("stopped" in state and type(state["stopped"]) is not bool
            or state.get("error") is not None and not isinstance(state["error"], str)):
        _invalid()
    request_id, fingerprint = state.get("request_id", ""), state.get("request_fingerprint", "")
    if len(request_id) > 80 or (fingerprint and not re.fullmatch(r"[0-9a-f]{64}", fingerprint)) or request_id and not fingerprint:
        _invalid()
    results = state.get("results", [])
    if type(results) is not list:
        _invalid()
    created = 0
    emails = set()
    for row in results:
        try:
            valid = (type(row) is dict and not set(row) - _RESULT_FIELDS
                     and credential_store.email_key(row.get("email")) == row.get("email")
                     and type(row.get("created")) is bool
                     and (row.get("error") is None or isinstance(row["error"], str))
                     and (not row["created"] or row.get("error") is None))
            if valid:
                valid = (all(isinstance(row[key], str) for key in ("nome", "sobrenome") if key in row)
                         and all(row[key] is None or isinstance(row[key], str) for key in ("gender",) if key in row)
                         and all(row[key] is None or type(row[key]) is int for key in ("birth_month", "birth_year") if key in row)
                         and all(type(row[key]) is bool for key in ("partial", "removable", "removed") if key in row))
                validate_steps(row.get("steps"))
        except (ValueError, TypeError):
            valid = False
        if not valid:
            _invalid()
        if row["email"] in emails:
            _invalid()
        emails.add(row["email"])
        created += int(row["created"])
    total, completed = state.get("total", 0), state.get("completed", 0)
    if (completed > total or completed != len(results) or state.get("created", 0) != created
            or state.get("failed", 0) != len(results) - created
            or state["state"] == "done" and completed < total and state.get("stopped") is not True):
        _invalid()
    return state


def load(path: Path) -> dict:
    return validate(load_json_state(path, {"state": "idle"}))


def save(path: Path, state: dict) -> None:
    validate(state)
    load(path)  # Existing corrupt progress is never treated as an empty cache.
    save_json(path, state)
