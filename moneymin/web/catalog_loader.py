"""Bounded background reads for catalogs whose cold build exceeds HTTP timeouts."""
from __future__ import annotations

import threading
import time
import uuid
from typing import Callable


class CatalogLoader:
    def __init__(self, *, ttl_s: float = 30, max_pending: int = 2, timeout_s: float = 300,
                 timeout_message: str | None = None, max_cached: int = 32):
        self._lock = threading.Lock()
        self._worker = threading.Semaphore(1)
        self._jobs: dict[tuple, dict] = {}
        self._ttl_s = ttl_s
        self._max_pending = max_pending
        self._timeout_s = timeout_s
        self._max_runtime_s = max(timeout_s, 1800)
        self._max_cached = max(1, int(max_cached))
        self._timeout_message = timeout_message or (
            "A preparação das categorias excedeu o tempo esperado. "
            "O cálculo continua no serviço. Tente recarregar em instantes.")

    @property
    def busy(self) -> bool:
        with self._lock:
            return any("finished" not in row for row in self._jobs.values())

    def clear_idle(self) -> None:
        """Discard cached operational results only after every worker returns."""
        with self._lock:
            if any("finished" not in row for row in self._jobs.values()):
                raise RuntimeError("Aguarde a consulta terminar antes do reset.")
            self._jobs.clear()

    def get(self, key: tuple, work: Callable, *, scope: str | None = None, refresh: bool = False) -> tuple[dict, int]:
        with self._lock:
            now = time.monotonic()
            self._expire(now)
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
                       "job_id": uuid.uuid4().hex, "phase": "queued",
                       "message": "Aguardando a preparação do catálogo."}
                self._jobs[key] = row
                try:
                    threading.Thread(target=self._run, args=(row, work), daemon=True,
                                     name="qmoney-task-catalog").start()
                except RuntimeError:
                    del self._jobs[key]
                    return {"error": "Não foi possível iniciar a consulta. Tente novamente."}, 503
            return self._response(self._jobs[key], now)

    def poll(self, job_id: str, *, key: tuple, scope: str | None = None) -> tuple[dict, int]:
        """Read only the selected, still-authorized job; never launch another build."""
        with self._lock:
            now = time.monotonic()
            self._expire(now)
            for stored_key, row in self._jobs.items():
                if row["job_id"] != job_id:
                    continue
                # The caller supplies current selection AND account identity.
                # A removed/replaced credential cannot claim an older result.
                if stored_key != key or row.get("scope") != scope:
                    return {"error": "A seleção ou o acesso da conta mudou. Recarregue as categorias.",
                            "code": "catalog_identity_changed"}, 409
                return self._response(row, now)
            return {"error": "A consulta anterior não está mais disponível. Recarregue as categorias.",
                    "code": "catalog_job_unavailable"}, 409

    def poll_account_exclusion_diagnostic(self, job_id: str, *, key_prefix: tuple,
                                          scope: str | None) -> tuple[dict, int] | None:
        """Return only this account's archived restriction status after token removal.

        This intentionally does not relax :meth:`poll`: callers must separately
        confirm the canonical ban record and absent token before using this
        narrow channel. While the worker is still returning from that archive,
        it exposes only pending progress; after completion it exposes only the
        typed restriction diagnostic. Success results and other errors cannot
        cross an identity fingerprint change.
        """
        if (scope != "campaign-tasks" or not isinstance(key_prefix, tuple)
                or len(key_prefix) != 5 or not isinstance(key_prefix[0], str)):
            return None
        with self._lock:
            now = time.monotonic()
            self._expire(now)
            for stored_key, row in self._jobs.items():
                if row.get("job_id") != job_id:
                    continue
                if (row.get("scope") != scope or len(stored_key) != 6
                        or stored_key[:5] != key_prefix or stored_key[0] != key_prefix[0]
                        or row.get("state") not in {"queued", "running"}):
                    return None
                if "finished" not in row:
                    # The ban path has already removed the token, but the
                    # worker has not yet returned its typed result. Do not
                    # leak an in-flight result or expose ordinary progress
                    # text through this identity-change exception.
                    return ({"loading": True, "state": row["state"],
                             "message": "Finalizando o registro da conta removida.",
                             "phase": "archiving_removed_account",
                             "job_id": row["job_id"], "identity_bound": True,
                             "elapsed_s": max(0, int(now - row["started"]))}, 202)
                result, status = row.get("result", ({}, 500))
                if (status != 400 or not isinstance(result, dict)
                        or result.get("code") != "catalog_account_unavailable"
                        or result.get("permanently_removed") is not True
                        or result.get("removal_failed") is True or "tasks" in result):
                    return None
                issue = result.get("issue")
                if (not isinstance(issue, dict) or issue.get("email") != key_prefix[0]
                        or issue.get("code") != "restricted"
                        or issue.get("stage") != "Carregamento remoto de categorias"
                        or issue.get("restriction_confirmed") is not True
                        or not isinstance(issue.get("reason"), str)
                        or not isinstance(issue.get("action"), str)):
                    return None
                safe_issue = {field: issue[field] for field in
                              ("email", "code", "stage", "reason", "action", "restriction_confirmed")}
                row.setdefault("delivered", now)
                return ({"error": safe_issue["reason"] + " " + safe_issue["action"],
                         "code": "catalog_account_unavailable", "issue": safe_issue,
                         "permanently_removed": True}, 400)
            return None

    def _expire(self, now: float) -> None:
        # An unobserved terminal result survives the normal TTL, including
        # after a 504 paused the UI. Bound this retention by result count.
        for old_key, row in list(self._jobs.items()):
            if row.get("delivered") is not None and now - row["delivered"] >= self._ttl_s:
                del self._jobs[old_key]
        completed = sorted(((key, row) for key, row in self._jobs.items()
                            if "finished" in row), key=lambda item: item[1]["finished"])
        for old_key, _ in completed[:-self._max_cached]:
            del self._jobs[old_key]

    def _response(self, row: dict, now: float) -> tuple[dict, int]:
        if "finished" in row:
            row.setdefault("delivered", now)
            return row["result"]
        body = {"loading": True, "state": row["state"], "message": row["message"],
                "phase": row["phase"], "job_id": row["job_id"], "identity_bound": True,
                "elapsed_s": int(now - row["started"])}
        if (now - row.get("last_progress", row["started"]) >= self._timeout_s
                or now - row["started"] >= self._max_runtime_s):
            # A 504 is visible, but explicitly describes a live worker so a
            # bounded UI follow-up can recover it without starting another.
            body.update(error=self._timeout_message, code="catalog_work_pending")
            return body, 504
        return body, 202

    def _run(self, row: dict, work: Callable) -> None:
        def progress(message, *, phase: str | None = None):
            with self._lock:
                if message != row.get("message") or (phase and phase != row.get("phase")):
                    row["last_progress"] = time.monotonic()
                row.update(state="running", message=message, phase=phase or message)
        with self._worker:
            with self._lock:
                if row.get("cancelled"):
                    return
                row.update(state="running", phase="waiting_local_state",
                           message="Aguardando acesso ao estado local…")
            try:
                # Operational catalog reads can migrate journals or retain a
                # campaign preview. Reset must wait for their actual lifetime.
                from ..campaign_state import campaign_state_lease
                from ..background_work import responsive_catalog_work
                with campaign_state_lease(), responsive_catalog_work(progress=progress):
                    result = work(progress)
            except Exception:
                # Never leave polling stuck forever, or expose credentials in
                # an unexpected exception. Expected errors are mapped by work.
                result = ({"error": "A consulta falhou. Tente recarregar."}, 500)
            with self._lock:
                row.update(result=result, finished=time.monotonic())
                self._expire(time.monotonic())
