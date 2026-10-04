"""OS-held exclusivity for service processes sharing an explicit state root.

The marker contains no identity or credentials and is deliberately retained.
The OS releases its byte/file lock when the holding process exits, including
crashes. This lease does not make multiple-file writes transactional, and
standalone legacy CLIs are outside the service's exclusivity contract.
"""
from contextlib import contextmanager
import os
from pathlib import Path


class StateLeaseError(RuntimeError):
    """The state root cannot be claimed exclusively by this service."""


@contextmanager
def service_state_lease(root: Path):
    stream = None
    locked = False
    try:
        root = Path(root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        stream = (root / "service-state.lock").open("a+b")
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
    except (OSError, ValueError):
        if stream is not None:
            stream.close()
        raise StateLeaseError(
            "Não foi possível reservar o estado local. Feche outra instância "
            "do serviço que use estes dados ou verifique as permissões antes de tentar novamente."
        ) from None
    try:
        yield
    finally:
        if locked:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()


__all__ = ["StateLeaseError", "service_state_lease"]
