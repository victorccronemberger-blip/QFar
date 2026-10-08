"""Crash-released local operation leases, including threads sharing a root.

The file is retained. Its existence/PID is not ownership. This is one lock,
not an atomic transaction spanning journals, provider effects and history.
"""
from contextlib import contextmanager
import os
import errno
import math
from pathlib import Path
import threading

class OperationLeaseError(ValueError):
    def __init__(self, message, *, busy=False):
        super().__init__(message)
        self.busy = bool(busy)

_GUARD = threading.Lock()
_LOCKS = {}
_LOCAL = threading.local()

def _is_lock_conflict(exc):
    # This helper is called only around the kernel locking call, after the
    # lease file has been opened. EACCES is a lock conflict for msvcrt on
    # Windows; open/permission errors are handled outside this helper.
    if getattr(exc, 'winerror', None) in {32, 33}:
        return True
    return getattr(exc, 'errno', None) in {
        errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK,
        getattr(errno, 'EDEADLK', -1),
    }

@contextmanager
def operation_lease(path, *, wait_local=False, timeout_s=0.0):
    path = Path(path).resolve()
    key = os.path.normcase(str(path))
    with _GUARD:
        lock = _LOCKS.setdefault(key, threading.RLock())
    if type(wait_local) is not bool:
        raise ValueError('Modo de espera da operação local inválido.')
    if wait_local:
        if (type(timeout_s) not in (int, float) or not math.isfinite(timeout_s)
                or timeout_s < 0):
            raise ValueError('Tempo de espera da operação local inválido.')
        acquired_local = lock.acquire(timeout=timeout_s)
    else:
        acquired_local = lock.acquire(blocking=False)
    if not acquired_local:
        raise OperationLeaseError('Uma operação local está em andamento.', busy=True)
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
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if _is_lock_conflict(exc):
                    raise OperationLeaseError(
                        'Uma operação local está em andamento.', busy=True) from None
                raise
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if _is_lock_conflict(exc):
                    raise OperationLeaseError(
                        'Uma operação local está em andamento.', busy=True) from None
                raise
        acquired = True
        held.add(key)
        yield
    except OperationLeaseError:
        raise
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
