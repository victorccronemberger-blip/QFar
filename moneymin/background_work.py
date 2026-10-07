"""Cooperative CPU budget for background catalog reads sharing the local API."""
from __future__ import annotations

import contextvars
import time
from contextlib import contextmanager

_BUDGET = contextvars.ContextVar("qmoney_background_cpu_budget", default=None)
_SLICE_S = .01
_YIELD_S = .001


@contextmanager
def responsive_catalog_work():
    """Let local request threads run without changing classification results."""
    if _BUDGET.get() is not None:
        yield
        return
    token = _BUDGET.set([time.monotonic() + _SLICE_S])
    try:
        yield
    finally:
        _BUDGET.reset(token)


def checkpoint() -> None:
    budget = _BUDGET.get()
    if budget is not None and time.monotonic() >= budget[0]:
        # sleep(0) can immediately reacquire the GIL on Windows. A short real
        # wait permits filesystem/DPAPI callbacks and HTTP dispatch to proceed.
        time.sleep(_YIELD_S)
        budget[0] = time.monotonic() + _SLICE_S
