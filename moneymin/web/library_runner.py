"""Cooperative local Library acquisition; never a campaign or an upload."""
from __future__ import annotations

import copy
import threading
import uuid
from typing import Callable

from ..campaign_state import campaign_state_lease
from ..nymeria_library import NymeriaLibraryError


class LibraryPreparationRunner:
    def __init__(self):
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._state = {"state": "idle", "operation": None, "message": "", "result": None}

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def snapshot(self) -> dict:
        with self._lock:
            return {**copy.deepcopy(self._state), "running": self.running,
                    "stop_requested": self._stop.is_set()}

    def stop(self) -> None:
        with self._lock:
            if self.running:
                self._stop.set()
                self._state["message"] = "Parando a preparação; os arquivos verificados serão preservados."

    def start(self, operation: str, work: Callable, *, root: str) -> dict:
        with self._lock:
            if self.running:
                raise RuntimeError("Aguarde a preparação da Biblioteca terminar.")
            self._stop.clear()
            self._state = {"state": "running", "operation": operation, "root": root,
                           "run_id": uuid.uuid4().hex, "message": "Preparando Biblioteca Nymeria…",
                           "result": None}
            self._thread = threading.Thread(target=self._run, args=(work,), daemon=True,
                                            name="qmoney-nymeria-library")
            self._thread.start()
            return self.snapshot()

    def _run(self, work: Callable) -> None:
        def progress(value):
            # Provider messages must be safe summaries, never signed URLs.
            with self._lock:
                if isinstance(value, dict):
                    allowed = {key: value[key] for key in
                               ("message", "completed", "total", "bytes_done", "bytes_total", "sequence_id")
                               if key in value}
                    self._state.update(allowed)
                else:
                    self._state["message"] = str(value)
        try:
            with campaign_state_lease():
                result = work(progress, self._stop.is_set)
            with self._lock:
                stopped = self._stop.is_set()
                self._state.update(state="stopped" if stopped else "done", result=result,
                                   message="Preparação interrompida; arquivos verificados preservados."
                                   if stopped else "Preparação da Biblioteca concluída.")
        except Exception as exc:
            with self._lock:
                stopped = self._stop.is_set()
                safe_message = (exc.safe_message if isinstance(exc, NymeriaLibraryError) else
                                "Não foi possível concluir a preparação. Confira o manifesto, a conexão e o espaço livre.")
                self._state.update(state="stopped" if stopped else "error", result=None,
                                   message="Preparação interrompida; arquivos verificados preservados."
                                   if stopped else safe_message,
                                   error_code=exc.code if isinstance(exc, NymeriaLibraryError) else "library_preparation_failed")
