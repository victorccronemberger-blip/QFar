"""Short exclusive barrier for media protection publication and cleanup.

The barrier does not cover HTTP requests or serialize different recording
uploads. A cleanup cannot race the initial durable journal/start reservation.
"""
from functools import wraps
from contextlib import contextmanager, ExitStack
import time
import math
from . import config
from .operation_lease import operation_lease, OperationLeaseError

@contextmanager
def media_state_lease(*, wait=False, timeout_s=2.0):
    # Different recordings may publish short checkpoints simultaneously.
    # Wait only for this local critical section; HTTP and SID ownership never
    # use this wait. Cleanup keeps its nonblocking, fail-closed behavior.
    if (type(timeout_s) not in (int, float) or not math.isfinite(timeout_s)
            or timeout_s < 0):
        raise ValueError('Tempo de espera da operação local inválido.')
    deadline = time.monotonic() + timeout_s
    stack = ExitStack()
    while True:
        try:
            stack.enter_context(operation_lease(config.DATA_DIR/'.media-lifecycle.lock'))
            break
        except OperationLeaseError:
            if not wait or time.monotonic() >= deadline:
                stack.close()
                raise
            time.sleep(0.01)
    try:
        yield
    finally:
        stack.close()

def cleanup_operation(fn):
    @wraps(fn)
    def guarded(*args,**kwargs):
        with media_state_lease():
            return fn(*args,**kwargs)
    return guarded
