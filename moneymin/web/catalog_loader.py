"""Bounded background reads for catalogs whose cold build exceeds HTTP timeouts."""
from __future__ import annotations

import threading
import time
from typing import Callable


class CatalogLoader:
    def __init__(self, *, ttl_s: float = 30, max_pending: int = 2, timeout_s: float = 300,
                 timeout_message: str | None = None):
        self._lock = threading.Lock()
        self._worker = threading.Semaphore(1)
        self._jobs: dict[tuple, dict] = {}
        self._ttl_s = ttl_s
        self._max_pending = max_pending
        self._timeout_s = timeout_s
        self._timeout_message = timeout_message or (
            "A preparação das categorias excedeu o tempo esperado. "
            "O cálculo continua no serviço. Tente recarregar em instantes.")

    def get(self, key: tuple, work: Callable, *, scope: str | None = None, refresh: bool = False) -> tuple[dict, int]:
        with self._lock:
            now = time.monotonic()
            for old_key, row in list(self._jobs.items()):
                if row.get("finished") is not None and now - row["finished"] >= self._ttl_s:
                    del self._jobs[old_key]
            # Explicit retries replace terminal cached results, never a read
            # still queued/running. Repeated clicks cannot duplicate workers.
            if refresh and key in self._jobs and "finished" in self._jobs[key]:
                del self._jobs[key]
            if scope is not None:
                # Replace queued selections, never launch concurrent catalog builds.
                for old_key, old_row in list(self._jobs.items()):
                    if (old_key != key and old_row.get("scope") == scope
                            and old_row["state"] == "queued"):
                        old_row["cancelled"] = True
                        del self._jobs[old_key]
            if key not in self._jobs:
                if sum("finished" not in row for row in self._jobs.values()) >= self._max_pending:
                    return {"loading": True, "state": "queued",
                            "message": "Aguardando a preparação do catálogo em andamento."}, 202
                row = {"state": "queued", "started": now, "scope": scope,
                       "message": "Aguardando a preparação do catálogo."}
                self._jobs[key] = row
                try:
                    threading.Thread(target=self._run, args=(row, work), daemon=True,
                                     name="qmoney-task-catalog").start()
                except RuntimeError:
                    del self._jobs[key]
                    return {"error": "Não foi possível iniciar a consulta. Tente novamente."}, 503
            row = self._jobs[key]
            if "finished" in row:
                return row["result"]
            if now - row["started"] >= self._timeout_s:
                # Keep the worker registered: a retry must not spawn duplicates.
                return {"error": self._timeout_message}, 504
            return {"loading": True, "state": row["state"], "message": row["message"],
                    "elapsed_s": int(now - row["started"])}, 202

    def _run(self, row: dict, work: Callable) -> None:
        def progress(message):
            with self._lock:
                row.update(state="running", message=message)
        with self._worker:
            with self._lock:
                if row.get("cancelled"):
                    return
                row["state"] = "running"
            try:
                result = work(progress)
            except Exception:
                # Never leave polling stuck forever, or expose credentials in
                # an unexpected exception. Expected errors are mapped by work.
                result = ({"error": "A consulta falhou. Tente recarregar."}, 500)
            with self._lock:
                row.update(result=result, finished=time.monotonic())
