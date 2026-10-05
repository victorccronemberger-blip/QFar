"""Durable registration checkpoints. Passwords stay in the credential store."""
from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from .. import config, credential_store
from ..atomic_io import JsonStateError, load_json_state, save_json

_LOCK = threading.RLock()
STEPS = {"ban_check", "save_partial", "crowtado_signup", "demographics",
         "minute_register", "link_minute", "validate"}


def load() -> dict:
    with _LOCK:
        data = load_json_state(config.DATA_DIR / "account_registrations.json", {})
        for email, row in data.items():
            try:
                valid = (credential_store.email_key(email) == email and isinstance(row, dict)
                         and set(row) <= {"email", "state", "identity", "steps", "error", "updated_at"}
                         and row.get("email") == email
                         and row.get("state") in {"running", "incomplete", "complete"}
                         and isinstance(row.get("steps"), dict)
                         and isinstance(row.get("identity"), dict))
                if valid:
                    identity = row["identity"]
                    valid = (set(identity) <= {"nome", "sobrenome", "gender", "birth_month", "birth_year", "use_referral"}
                             and ("use_referral" not in identity or type(identity["use_referral"]) is bool)
                             and all(isinstance(identity[k], str) for k in ("nome", "sobrenome", "gender") if k in identity)
                             and all(type(identity[k]) is int for k in ("birth_month", "birth_year") if k in identity)
                             and (row.get("error") is None or isinstance(row.get("error"), str))
                             and all(key in STEPS and isinstance(step, dict) and set(step) <= {"status", "detail", "code"}
                                and step.get("status") in {"ok", "skip", "fail"}
                                and ("code" not in step or isinstance(step["code"], str))
                                and isinstance(step.get("detail", ""), str)
                                for key, step in row["steps"].items()))
            except ValueError:
                valid = False
            if not valid:
                raise JsonStateError("O histórico de cadastros está inválido; preserve o arquivo antes de continuar.")
        return data


def update(email: str, *, identity=None, steps=None, state="running", error=None) -> dict:
    key = credential_store.email_key(email)
    with _LOCK:
        rows = load()
        previous = rows.get(key, {})
        row = {"email": key, "state": state,
               "identity": {k: v for k, v in (identity or previous.get("identity", {})).items()
                            if k in {"nome", "sobrenome", "gender", "birth_month", "birth_year", "use_referral"}},
               "steps": copy.deepcopy(steps if steps is not None else previous.get("steps", {})),
               "error": error, "updated_at": datetime.now(timezone.utc).isoformat()}
        rows[key] = row
        save_json(config.DATA_DIR / "account_registrations.json", rows)
        return row
