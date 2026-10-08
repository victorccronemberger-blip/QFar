"""Durable provider-wide creation cooldowns; no requests or account mutations."""
from __future__ import annotations

import math
import threading
import time
from pathlib import Path

from .. import config
from ..atomic_io import JsonStateError, load_json_state, save_json
from . import registration_state

PROVIDERS = ("crowtado", "minute")
DEFAULT_COOLDOWN_SECONDS = 300
_LOCK = threading.RLock()
# An observed provider limit remains binding if its durable publication fails.
# Roots are part of the key: another installation/test cannot inherit this state.
_observed: dict[tuple[Path, str], float] = {}


def _path():
    return config.DATA_DIR / "account_registration_limits.json"


def _provider(provider: str) -> str:
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise ValueError("Provedor de cadastro inválido.") from None
    return provider


def _finite_nonnegative(value) -> bool:
    try:
        return type(value) in (int, float) and value >= 0 and math.isfinite(value)
    except (ValueError, OverflowError):
        return False


def _validate(document) -> dict:
    if (type(document) is not dict or set(document) != {"schema", "providers"}
            or type(document["schema"]) is not int or document["schema"] != 1
            or type(document["providers"]) is not dict
            or set(document["providers"]) - set(PROVIDERS)
            or any(type(row) is not dict or set(row) != {"until_epoch_s"}
                   or not _finite_nonnegative(row["until_epoch_s"])
                   for row in document["providers"].values())):
        raise JsonStateError(
            "O intervalo de cadastros está inválido; preserve o arquivo antes de continuar."
        ) from None
    return document


def _load() -> dict:
    return _validate(load_json_state(_path(), {"schema": 1, "providers": {}}))


def _effective() -> dict:
    document = _load()  # Corrupt cooldowns never fall back to RAM/checkpoints.
    registrations = registration_state.load()  # Validate every consulted owner/step.
    deadlines = {provider: _observed.get((_path().resolve(), provider), 0)
                 for provider in PROVIDERS}
    for row in registrations.values():
        for name, step in row["steps"].items():
            if step.get("code") == "rate_limit" and "retry_at" in step:
                provider = "minute" if name in {"minute_identity", "minute_register", "validate"} else "crowtado"
                deadlines[provider] = max(deadlines[provider], step["retry_at"])
    for provider, deadline in deadlines.items():
        old = document["providers"].get(provider, {}).get("until_epoch_s", 0)
        if deadline > old:
            document["providers"][provider] = {"until_epoch_s": deadline}
    return _validate(document)


def _now() -> float:
    value = time.time()
    if not _finite_nonnegative(value):
        raise ValueError("O relógio local não permitiu conferir o intervalo de cadastros.") from None
    return value


def _remaining(document: dict, provider: str, now: float) -> int:
    deadline = document["providers"].get(provider, {}).get("until_epoch_s", 0)
    return max(0, math.ceil(deadline - now))


def defer(provider: str, retry_after_seconds=None) -> int:
    """Preserve/extend the provider deadline; absent Retry-After waits 300 s.

    An explicit zero is respected. A shorter later response cannot end a
    cooldown that is already active. The returned delay is rounded upward.
    """
    provider = _provider(provider)
    seconds = DEFAULT_COOLDOWN_SECONDS if retry_after_seconds is None else retry_after_seconds
    if not _finite_nonnegative(seconds):
        raise ValueError("Intervalo de cadastro inválido.") from None
    with _LOCK:
        document = _effective()
        now = _now()
        deadline = now + seconds
        if not _finite_nonnegative(deadline):
            raise ValueError("Intervalo de cadastro inválido.") from None
        old = document["providers"].get(provider, {}).get("until_epoch_s", 0)
        deadline = max(old, deadline)
        _observed[(_path().resolve(), provider)] = deadline
        document["providers"][provider] = {"until_epoch_s": deadline}
        _validate(document)
        save_json(_path(), document)
        return _remaining(document, provider, now)


def remaining(provider: str) -> int:
    """Return the strongest observed deadline, rounded upward, without writes."""
    provider = _provider(provider)
    with _LOCK:
        return _remaining(_effective(), provider, _now())


def check_all() -> tuple[str, int] | None:
    """Use one snapshot; Crowtado precedes Minute when both are deferred."""
    with _LOCK:
        document, now = _effective(), _now()
        for provider in PROVIDERS:
            seconds = _remaining(document, provider, now)
            if seconds:
                return provider, seconds
    return None
