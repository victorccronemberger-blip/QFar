"""Cotação USD/BRL oficial com cache local e fallback offline."""
from __future__ import annotations

import datetime as dt
import json
import math
import threading
import time
import urllib.request
from typing import Any, Callable

from . import config, tls
from .atomic_io import load_json, save_json

BCB_USD_BRL_URL = (
    "https://api.bcb.gov.br/dados/serie/bcdata.sgs.1/"
    "dados/ultimos/1?formato=json"
)
CACHE_PATH = config.DATA_DIR / "usd_brl_quote.json"
CACHE_TTL_S = 6 * 60 * 60
_LOCK = threading.Lock()
_LAST_FAILURE: tuple[str, int] | None = None
RETRY_DELAY_S = 60


def _offline_quote(cached: Any) -> dict[str, Any]:
    if _valid_quote(cached):
        return {**cached, "available": True, "stale": True}
    return {"available": False, "rate": None, "quote_date": None,
            "fetched_at": None, "source": "Banco Central do Brasil", "stale": True}


def _valid_quote(value: Any) -> bool:
    try:
        rate = float(value.get("rate"))
        fetched_at = int(value.get("fetched_at"))
    except (AttributeError, TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(rate) and 0.5 < rate < 20.0 and fetched_at > 0


def _parse_bcb(payload: Any, fetched_at: int) -> dict[str, Any]:
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise ValueError("resposta do BCB sem cotação")
    item = payload[0]
    rate = float(str(item.get("valor") or "").replace(",", "."))
    if not math.isfinite(rate) or not 0.5 < rate < 20.0:
        raise ValueError("cotação USD/BRL inválida")
    raw_date = str(item.get("data") or "")
    quote_date = dt.datetime.strptime(raw_date, "%d/%m/%Y").date().isoformat()
    return {
        "available": True,
        "rate": rate,
        "quote_date": quote_date,
        "fetched_at": fetched_at,
        "source": "Banco Central do Brasil",
        "stale": False,
    }


def usd_brl_quote(
    *,
    now: int | None = None,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Devolve BRL por USD; usa cache por 6 h e o preserva quando offline."""
    global _LAST_FAILURE
    current = int(time.time() if now is None else now)
    cached = load_json(CACHE_PATH, {})
    if _valid_quote(cached) and 0 <= current - int(cached["fetched_at"]) < CACHE_TTL_S:
        return {**cached, "available": True, "stale": False}

    # A stalled quote must not queue every balance poll behind network I/O.
    if not _LOCK.acquire(blocking=False):
        return _offline_quote(cached)
    try:
        cached = load_json(CACHE_PATH, {})
        if _valid_quote(cached) and 0 <= current - int(cached["fetched_at"]) < CACHE_TTL_S:
            return {**cached, "available": True, "stale": False}
        if (_LAST_FAILURE is not None and _LAST_FAILURE[0] == str(CACHE_PATH)
                and 0 <= current - _LAST_FAILURE[1] < RETRY_DELAY_S):
            return _offline_quote(cached)
        try:
            request = urllib.request.Request(
                BCB_USD_BRL_URL,
                headers={"Accept": "application/json", "User-Agent": "QMoney/1"},
            )
            open_url = opener or tls.urlopen
            with open_url(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8-sig"))
            quote = _parse_bcb(payload, current)
            try:
                save_json(CACHE_PATH, quote)
            except OSError:
                pass  # The remote quote is valid even if the cache is unwritable.
            _LAST_FAILURE = None
            return quote
        except (OSError, TimeoutError, ValueError, TypeError, json.JSONDecodeError):
            _LAST_FAILURE = (str(CACHE_PATH), current)
            return _offline_quote(cached)
    finally:
        _LOCK.release()


__all__ = ["BCB_USD_BRL_URL", "CACHE_PATH", "usd_brl_quote"]
