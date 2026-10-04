"""Migração das contas cadastradas, com progresso e relatório sem credenciais."""
from __future__ import annotations

import copy
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..atomic_io import JsonStateError, load_json_state, save_json
from ..org_policy import account_kind
from .account_issues import account_issue


class OrgMigrationRunner:
    def __init__(self, report_path: Path):
        self.report_path = report_path
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        try:
            self._state = load_json_state(report_path, {})
        except JsonStateError:
            # Keep the desktop available for reviewing a damaged receipt,
            # while refusing a new migration until it is explicitly repaired.
            self._state = {"state": "needs_review", "total": 0, "completed": 0,
                           "results": [], "counts": {}, "can_start": False,
                           "error_code": "LOCAL_STATE_REVIEW_REQUIRED",
                           "error": "Relatório local inválido ou ilegível; preserve o arquivo e restaure um backup válido."}
        if self._state.get("state") == "running":
            self._state["state"] = "interrupted"

    @property
    def running(self) -> bool:
        with self._lock:
            return self._state.get("state") == "running"

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._state or {
                "state": "idle", "total": 0, "completed": 0, "results": [], "counts": {},
            })

    def start(self, emails: list[str], migrate: Callable[[str], dict]) -> dict:
        with self._lock:
            load_json_state(self.report_path, {})
            if self.running:
                raise RuntimeError("Uma migração já está em andamento.")
            self._state = {
                "state": "running", "total": len(emails), "completed": 0,
                "results": [], "counts": {}, "current_email": "",
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            # Falha de disco precisa ser conhecida antes de qualquer alteração remota.
            try:
                save_json(self.report_path, self._state)
                self._thread = threading.Thread(
                    target=self._run, args=(emails, migrate), daemon=True,
                    name="qmoney-org-migration",
                )
                self._thread.start()
            except Exception:
                self._state["state"] = "error"
                raise
            return self.snapshot()

    def _run(self, emails: list[str], migrate: Callable[[str], dict]) -> None:
        try:
            for email in emails:
                with self._lock:
                    self._state["current_email"] = email
                try:
                    row = migrate(email)
                except Exception as exc:  # noqa: BLE001 — isolar cada conta
                    issue = account_issue(email, exc, stage="Migração da organização")
                    row = {
                        "email": email,
                        "account_kind": account_kind(email),
                        "status": "restricted" if issue["code"] == "restricted" else "error",
                        "message": issue["reason"], "issue": issue,
                    }
                with self._lock:
                    self._state["results"].append(row)
                    self._state["completed"] += 1
                    self._state["counts"] = dict(Counter(
                        result["status"] for result in self._state["results"]))
                    save_json(self.report_path, self._state)
            with self._lock:
                self._state.update(
                    state="completed", current_email="",
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
                save_json(self.report_path, self._state)
        except Exception:  # noqa: BLE001 — liberar a UI sem expor erros brutos
            with self._lock:
                self._state.update(
                    state="error", current_email="",
                    error="A migração foi interrompida ao salvar o relatório. Confira o espaço e as permissões; depois tente novamente.",
                )
