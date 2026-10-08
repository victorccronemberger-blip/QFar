"""Durable registration checkpoints. Passwords stay in the credential store."""
from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from .. import config, credential_store
from ..atomic_io import JsonStateError, load_json_state, save_json

_LOCK = threading.RLock()
STEPS = {"proxy", "ban_check", "save_partial", "crowtado_signup", "demographics",
         "minute_register", "link_minute", "validate"}


def validate_steps(steps) -> dict:
    if (type(steps) is not dict or any(
            key not in STEPS or type(step) is not dict or set(step) - {"status", "detail", "code"}
            or not isinstance(step.get("status"), str) or step["status"] not in {"ok", "skip", "fail", "manual"}
            or ("code" in step and not isinstance(step["code"], str))
            or not isinstance(step.get("detail", ""), str)
            for key, step in steps.items())):
        raise JsonStateError("O histórico de cadastros está inválido; preserve o arquivo antes de continuar.")
    return steps


def validate(data) -> dict:
    if type(data) is not dict:
        raise JsonStateError("O histórico de cadastros está inválido; preserve o arquivo antes de continuar.")
    for email, row in data.items():
        try:
            valid = (credential_store.email_key(email) == email and type(row) is dict
                     and set(row) <= {"email", "state", "identity", "steps", "error", "updated_at"}
                     and row.get("email") == email
                     and row.get("state") in {"running", "incomplete", "complete"}
                     and type(row.get("identity")) is dict)
            if valid:
                identity = row["identity"]
                valid = (set(identity) <= {"nome", "sobrenome", "gender", "birth_month", "birth_year", "use_referral", "proxy_id"}
                         and ("use_referral" not in identity or type(identity["use_referral"]) is bool)
                         and all(isinstance(identity[k], str) for k in ("nome", "sobrenome", "gender", "proxy_id") if k in identity)
                         and all(type(identity[k]) is int for k in ("birth_month", "birth_year") if k in identity)
                         and (row.get("error") is None or isinstance(row.get("error"), str))
                         and ("updated_at" not in row or isinstance(row["updated_at"], str)))
                validate_steps(row.get("steps"))
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise JsonStateError("O histórico de cadastros está inválido; preserve o arquivo antes de continuar.")
    return data


def load() -> dict:
    with _LOCK:
        return validate(load_json_state(config.DATA_DIR / "account_registrations.json", {}))


def update(email: str, *, identity=None, steps=None, state="running", error=None) -> dict:
    key = credential_store.email_key(email)
    with _LOCK:
        rows = load()
        previous = rows.get(key, {})
        selected_identity = previous.get("identity", {}) if identity is None else identity
        if type(selected_identity) is not dict:
            raise JsonStateError("O histórico de cadastros está inválido; preserve o arquivo antes de continuar.")
        row = {"email": key, "state": state,
               "identity": {k: v for k, v in selected_identity.items()
                            if k in {"nome", "sobrenome", "gender", "birth_month", "birth_year", "use_referral", "proxy_id"}},
               "steps": copy.deepcopy(steps if steps is not None else previous.get("steps", {})),
               "error": error, "updated_at": datetime.now(timezone.utc).isoformat()}
        rows[key] = row
        validate(rows)
        save_json(config.DATA_DIR / "account_registrations.json", rows)
        return row
