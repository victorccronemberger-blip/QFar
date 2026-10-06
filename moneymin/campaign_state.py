"""Shared campaign operations and an exclusive, nonblocking reset barrier.

The neutral lock file is retained. Shared operations may run concurrently,
including uploads for different accounts. Reset never waits for a writer while
holding a media/store lock; a busy root fails before its state is changed.
"""
from contextlib import contextmanager
from functools import wraps
import os
from pathlib import Path
import threading

from . import config
from .operation_lease import OperationLeaseError

_LOCAL = threading.local()


class CampaignStateLeaseError(OperationLeaseError):
    """A campaign operation/reset owns this root or it cannot be locked."""

    def __init__(self, message: str, *, code: str = "campaign_reset_busy"):
        self.code = code
        super().__init__(message)


@contextmanager
def campaign_state_lease(*, exclusive: bool = False, root: Path | None = None):
    if type(exclusive) is not bool:
        raise ValueError("Modo da operação de campanha inválido.")
    path = (Path(config.DATA_DIR if root is None else root).resolve()
            / ".campaign-state.lock")
    key = os.path.normcase(str(path))
    held = getattr(_LOCAL, "held", None)
    if held is None:
        held = _LOCAL.held = {}
    if key in held:
        if exclusive and not held[key]:
            raise CampaignStateLeaseError("Não é possível converter uma operação ativa em reset.")
        yield
        return
    stream = None
    locked = False
    kernel = overlap = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open("a+b")
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            import msvcrt

            class Overlapped(ctypes.Structure):
                _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                            ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                            ("hEvent", wintypes.HANDLE)]

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
            kernel.LockFileEx.restype = wintypes.BOOL
            kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
            kernel.UnlockFileEx.restype = wintypes.BOOL
            overlap = Overlapped()
            handle = msvcrt.get_osfhandle(stream.fileno())
            flags = 1 | (2 if exclusive else 0)  # FAIL_IMMEDIATELY | optional EXCLUSIVE.
            if not kernel.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlap)):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            import fcntl
            fcntl.flock(stream.fileno(), (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                        | fcntl.LOCK_NB)
        locked = True
        held[key] = exclusive
    except (OSError, ValueError):
        if stream is not None:
            stream.close()
        raise CampaignStateLeaseError(
            "Há uma operação de campanha ou reset em andamento; aguarde terminar.") from None
    try:
        if not exclusive:
            # The OS lock keeps reset from creating a marker between this
            # check and the operation. Exclusive retry and its nested reads
            # must remain available after an interrupted purge.
            from .campaign_reset import pending
            if pending():
                raise CampaignStateLeaseError(
                    "A limpeza local não terminou. Tente Reset completo novamente.",
                    code="campaign_reset_incomplete")
        yield
    finally:
        held.pop(key)
        try:
            if locked:
                if os.name == "nt":
                    import ctypes
                    kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlap))
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def campaign_state_operation(fn):
    """Acquire the root's shared lease before a function's existing locks."""
    @wraps(fn)
    def guarded(*args, **kwargs):
        with campaign_state_lease():
            return fn(*args, **kwargs)
    return guarded


__all__ = ["CampaignStateLeaseError", "campaign_state_lease", "campaign_state_operation"]
