"""Crash-released local operation leases, including threads sharing a root.

The file is retained. Its existence/PID is not ownership. This is one lock,
not an atomic transaction spanning journals, provider effects and history.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import threading

class OperationLeaseError(ValueError):
    pass

_GUARD = threading.Lock()
_LOCKS = {}
_LOCAL = threading.local()

@contextmanager
def operation_lease(path):
    path = Path(path).resolve()
    key = os.path.normcase(str(path))
    with _GUARD:
        lock = _LOCKS.setdefault(key, threading.RLock())
    if not lock.acquire(blocking=False):
        raise OperationLeaseError('Uma operação local está em andamento.')
    held = getattr(_LOCAL, 'held', None)
    if held is None:
        held = _LOCAL.held = set()
    if key in held:
        try:
            yield
        finally:
            lock.release()
        return
    stream = None
    acquired = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open('a+b')
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        held.add(key)
        yield
    except (OSError, ValueError) as exc:
        if not acquired:
            raise OperationLeaseError('Não foi possível reservar a operação local.') from None
        raise
    finally:
        if acquired:
            held.remove(key)
            try:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()
        elif stream is not None:
            stream.close()
        lock.release()
