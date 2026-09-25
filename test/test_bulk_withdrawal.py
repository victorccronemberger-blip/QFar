from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from moneymin.web import server


class BulkWithdrawalTests(unittest.TestCase):
    def test_wise_cleanup_runs_after_every_withdrawal_outcome(self):
        destination = {"legal_name": "Test Name", "destination_email": "destination@example.com"}
        for outcome in ({"status": "ok"}, {"status": "review_required"},
                        {"status": "below_minimum"}, TimeoutError("lost response")):
            with self.subTest(outcome=outcome):
                def configured(*args, **kwargs):
                    pending = server._wise_cleanup_snapshot()
                    self.assertTrue(pending["pending"])
                    self.assertEqual(pending["email"], "one@example.com")
                    self.assertNotIn("destination@example.com", str(pending))
                with patch.object(server.crowtado, "configurar_metodo_saque", side_effect=configured), \
                     patch.object(server.crowtado, "solicitar_link_saque", side_effect=[outcome]) as withdraw, \
                     patch.object(server.crowtado, "finalizar_wise", return_value={
                         "wiseDestinationRemoved": True, "payoutPreferenceRestored": True}) as cleanup:
                    result = server._withdraw_wise_flow("one@example.com", "pw", destination)
                withdraw.assert_called_once()
                cleanup.assert_called_once_with("one@example.com", "pw")
                self.assertFalse(result["cleanupPending"])
                self.assertFalse(server._wise_cleanup_snapshot()["pending"])

    def test_partial_link_failure_still_cleans_up(self):
        with patch.object(server.crowtado, "configurar_metodo_saque", side_effect=TimeoutError()), \
             patch.object(server.crowtado, "solicitar_link_saque") as withdraw, \
             patch.object(server.crowtado, "finalizar_wise", return_value={
                 "wiseDestinationRemoved": True, "payoutPreferenceRestored": True}) as cleanup:
            result = server._withdraw_wise_flow("one@example.com", "pw",
                {"legal_name": "Test Name", "destination_email": "destination@example.com"})
        cleanup.assert_called_once()
        withdraw.assert_not_called()
        self.assertFalse(result["cleanupPending"])

    def test_pending_cleanup_survives_restart_and_recovery_never_withdraws(self):
        server.save_json(server._wise_cleanup_path(), {"pending": True, "email": "one@example.com"})
        restarted_client = server.create_app().test_client()
        with patch.object(server.crowtado, "solicitar_link_saque") as withdraw, \
             patch.object(server.crowtado, "configurar_metodo_saque") as configure, \
             patch.object(server, "_configured_crowtado_creds", return_value={"one@example.com": "pw"}):
            _, code = server._withdraw_once("two@example.com", "pw")
            self.assertEqual(code, 409)
            self.assertEqual(restarted_client.post("/api/balances/withdraw-all").status_code, 409)
            self.assertEqual(restarted_client.post("/api/balances/payout-methods/apply-all",
                                                  json={"method": "dots"}).status_code, 409)
            recovery = restarted_client.post("/api/balances/wise-cleanup")
        self.assertEqual(recovery.status_code, 200)
        self.assertFalse(server._wise_cleanup_snapshot()["pending"])
        withdraw.assert_not_called()
        configure.assert_not_called()

    def test_cleanup_retries_only_cleanup_and_keeps_pending_if_remote_refuses(self):
        with patch.object(server.crowtado, "configurar_metodo_saque"), \
             patch.object(server.crowtado, "solicitar_link_saque", return_value={"status": "ok"}) as withdraw, \
             patch.object(server.crowtado, "finalizar_wise", return_value={
                 "wiseDestinationRemoved": False, "payoutPreferenceRestored": True}) as cleanup:
            result, code = server._withdraw_once("one@example.com", "pw",
                {"legal_name": "Test Name", "destination_email": "destination@example.com"})
        self.assertEqual(code, 200)
        self.assertTrue(result["ok"])
        self.assertIn("PENDENTE", result["message"])
        self.assertEqual(cleanup.call_count, 2)
        withdraw.assert_called_once()
        self.assertTrue(server._wise_cleanup_snapshot()["pending"])

    def test_unreadable_cleanup_state_blocks_new_withdrawals(self):
        server._wise_cleanup_path().write_text("{broken", encoding="utf-8")
        with patch.object(server.crowtado, "solicitar_link_saque") as withdraw:
            _, code = server._withdraw_once("one@example.com", "pw")
        self.assertEqual(code, 409)
        withdraw.assert_not_called()

    def test_cannot_link_without_durable_cleanup_record(self):
        with patch.object(server, "save_json", side_effect=OSError("disk full")), \
             patch.object(server.crowtado, "configurar_metodo_saque") as configure:
            with self.assertRaises(OSError):
                server._withdraw_wise_flow("one@example.com", "pw",
                    {"legal_name": "Test Name", "destination_email": "destination@example.com"})
        configure.assert_not_called()

    def test_wise_requires_confirmation_and_valid_destination(self):
        for route in ("/api/balances/withdraw", "/api/balances/withdraw-all"):
            for body in ({"method": "wise"},
                         {"method": "wise", "wise_confirmed": "true"},
                         {"method": "wise", "wise_confirmed": True,
                          "legal_name": "Name", "destination_email": "invalid"}):
                with self.subTest(route=route, body=body), \
                     patch.object(server.crowtado, "configurar_metodo_saque") as configure, \
                     patch.object(server.crowtado, "solicitar_link_saque") as withdraw:
                    self.assertEqual(self.client.post(route, json=body).status_code, 400)
                    configure.assert_not_called()
                    withdraw.assert_not_called()

    def test_confirmed_wise_batch_runs_five_accounts_sequentially(self):
        emails = [f"test-{i}@example.com" for i in range(5)]
        events = []
        destination = {"legal_name": "Test Name", "destination_email": "destination@example.com"}

        class ImmediateThread:
            def __init__(self, *, target, args, **kwargs):
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)

        def configure(email, password, method, **kwargs):
            self.assertEqual(method, "wise")
            self.assertEqual(kwargs, destination)
            events.append((email, "configure"))

        def withdraw(email, password, **kwargs):
            self.assertEqual(kwargs, {"expected_method": "wise", "cleanup_wise": False})
            events.append((email, "withdraw"))
            return {"status": "ok"}

        def cleanup(email, password):
            events.append((email, "cleanup"))
            return {"wiseDestinationRemoved": True, "payoutPreferenceRestored": True}

        with patch.object(server, "_list_accounts", return_value=[{"email": e} for e in emails]), \
             patch.object(server, "_configured_crowtado_creds", return_value=dict.fromkeys(emails, "pw")), \
             patch.object(server, "_load_balances", return_value={e: {"availableCents": 100} for e in emails}), \
             patch.object(server.org_policy, "account_kind", return_value="crowtado"), \
             patch.object(server.threading, "Thread", ImmediateThread), \
             patch.object(server.crowtado, "configurar_metodo_saque", side_effect=configure), \
             patch.object(server.crowtado, "finalizar_wise", side_effect=cleanup), \
             patch.object(server.crowtado, "solicitar_link_saque", side_effect=withdraw):
            response = self.client.post("/api/balances/withdraw-all", json={
                "method": "wise", "wise_confirmed": True, **destination})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(events, [(email, step) for email in emails
                                  for step in ("configure", "withdraw", "cleanup")])
        saved = server.load_json(server._withdraw_bulk_path(), {})
        self.assertEqual(saved["state"], "done")
        self.assertEqual(saved["done"], 5)
        self.assertEqual(saved["method"], "wise")
        self.assertNotIn(destination["destination_email"], str(saved))

    def test_wise_batch_stops_after_incomplete_cleanup_or_review(self):
        for detail in ({"status": "ok", "wiseDestinationRemoved": False, "payoutPreferenceRestored": True},
                       {"status": "ok", "wiseDestinationRemoved": True, "payoutPreferenceRestored": False},
                       {"status": "review_required"}, {"status": "below_minimum"}):
            with self.subTest(detail=detail):
                server._WITHDRAW_LAST_REQUEST.clear()
                server.save_json(server._wise_cleanup_path(), {"pending": False})
                server._WITHDRAW_BULK_STATE.update(state="running", total=2, done=0, results=[])
                with patch.object(server.crowtado, "configurar_metodo_saque") as configure, \
                     patch.object(server.crowtado, "finalizar_wise", return_value={
                         "wiseDestinationRemoved": detail.get("wiseDestinationRemoved", True),
                         "payoutPreferenceRestored": detail.get("payoutPreferenceRestored", True)}), \
                     patch.object(server.crowtado, "solicitar_link_saque", return_value=detail) as withdraw:
                    server._withdraw_bulk_run({"one@example.com": "pw", "two@example.com": "pw"},
                        {"legal_name": "Test Name", "destination_email": "destination@example.com"})
                self.assertEqual(server._withdraw_bulk_snapshot()["state"], "error")
                self.assertEqual(server._withdraw_bulk_snapshot()["done"], 1)
                self.assertEqual(server._withdraw_bulk_snapshot()["results"][0]["ok"],
                                 detail["status"] in {"ok", "review_required"})
                configure.assert_called_once()
                withdraw.assert_called_once()

    def test_wise_link_failure_stops_before_withdraw_and_redacts_destination(self):
        with patch.object(server.crowtado, "configurar_metodo_saque",
                          side_effect=RuntimeError("private@example.com private-name")), \
             patch.object(server.crowtado, "solicitar_link_saque") as withdraw:
            result, code = server._withdraw_once("one@example.com", "pw",
                {"legal_name": "private-name", "destination_email": "private@example.com"})
        self.assertEqual(code, 409)
        self.assertNotIn("private", str(result))
        withdraw.assert_not_called()

    def test_individual_wise_request_passes_confirmed_destination(self):
        email = "one@example.com"
        destination = {"legal_name": "Test Name", "destination_email": "destination@example.com"}
        with patch.object(server, "_list_accounts", return_value=[{"email": email}]), \
             patch.object(server, "_configured_crowtado_creds", return_value={email: "pw"}), \
             patch.object(server, "_load_balances", return_value={email: {"availableCents": 100}}), \
             patch.object(server.org_policy, "account_kind", return_value="crowtado"), \
             patch.object(server, "_withdraw_once", return_value=({"ok": True}, 200)) as withdraw:
            response = self.client.post("/api/balances/withdraw", json={
                "email": email, "method": "wise", "wise_confirmed": True, **destination})
        self.assertEqual(response.status_code, 200)
        withdraw.assert_called_once_with(email, "pw", destination)

    def test_concurrent_withdrawal_rejected_before_any_remote_change(self):
        with server._PAYOUT_OPERATION_LOCK, \
             patch.object(server.crowtado, "configurar_metodo_saque") as configure, \
             patch.object(server.crowtado, "solicitar_link_saque") as withdraw:
            _, code = server._withdraw_once("other@example.com", "pw",
                {"legal_name": "Test Name", "destination_email": "destination@example.com"})
        self.assertEqual(code, 409)
        configure.assert_not_called()
        withdraw.assert_not_called()

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        root = Path(self._temp.name)
        self._bulk_path_patch = patch.object(server, "_withdraw_bulk_path", return_value=root / "bulk.json")
        self._cooldown_path_patch = patch.object(server, "_withdraw_cooldown_path", return_value=root / "cooldown.json")
        self._cleanup_path_patch = patch.object(server, "_wise_cleanup_path", return_value=root / "cleanup.json")
        self._cleanup_path_patch.start()
        self.addCleanup(self._cleanup_path_patch.stop)
        self._finalizer_patch = patch.object(server.crowtado, "finalizar_wise", return_value={
            "wiseDestinationRemoved": True, "payoutPreferenceRestored": True})
        self._finalizer_patch.start()
        self.addCleanup(self._finalizer_patch.stop)
        self._bulk_path_patch.start()
        self._cooldown_path_patch.start()
        self.addCleanup(self._cooldown_path_patch.stop)
        self.addCleanup(self._bulk_path_patch.stop)
        self.client = server.create_app().test_client()
        self._previous_balance_state = server.BALANCES_RUNNER.state
        self._previous_bulk_loaded = server._WITHDRAW_BULK_LOADED
        self._previous_cooldown_loaded = server._WITHDRAW_COOLDOWN_LOADED
        server._WITHDRAW_BULK_LOADED = False
        server._WITHDRAW_COOLDOWN_LOADED = False
        server.BALANCES_RUNNER.state = "idle"
        server._WITHDRAW_LAST_REQUEST.clear()
        server._WITHDRAW_IN_FLIGHT.clear()
        with server._WITHDRAW_BULK_LOCK:
            server._WITHDRAW_BULK_STATE.update(state="idle", total=0, done=0, results=[])

    def tearDown(self):
        server.BALANCES_RUNNER.state = self._previous_balance_state
        server._WITHDRAW_BULK_LOADED = self._previous_bulk_loaded
        server._WITHDRAW_COOLDOWN_LOADED = self._previous_cooldown_loaded
        server._WITHDRAW_LAST_REQUEST.clear()
        server._WITHDRAW_IN_FLIGHT.clear()
        with server._WITHDRAW_BULK_LOCK:
            server._WITHDRAW_BULK_STATE.update(state="idle", total=0, done=0, results=[])

    def test_only_confirmed_available_crowtado_accounts_are_requested(self):
        accounts = [{"email": email} for email in (
            "good@example.com", "zero@example.com", "error@example.com",
            "claru@example.com", "unconnected@example.com")]
        balances = {
            "good@example.com": {"availableCents": 1200},
            "zero@example.com": {"availableCents": 0},
            "error@example.com": {"availableCents": 900, "error": "consulta falhou"},
            "claru@example.com": {"availableCents": 5000},
            "unconnected@example.com": {"availableCents": 200},
        }
        passwords = {email: "password" for email in (
            "good@example.com", "zero@example.com", "error@example.com",
            "claru@example.com")}
        submitted = []

        class ImmediateThread:
            def __init__(self, *, target, args, **kwargs):
                self.target, self.args = target, args

            def start(self):
                submitted.append(self.args[0])
                self.target(*self.args)

        with patch.object(server, "_list_accounts", return_value=accounts), \
             patch.object(server, "_configured_crowtado_creds", return_value=passwords), \
             patch.object(server, "_load_balances", return_value=balances), \
             patch.object(server.org_policy, "account_kind",
                          side_effect=lambda email: "claru" if email.startswith("claru") else "crowtado"), \
             patch.object(server.threading, "Thread", ImmediateThread), \
             patch.object(server.crowtado, "solicitar_link_saque", return_value={
                 "status": "ok", "dotsEmailDelivery": "sent"}) as withdraw:
            response = self.client.post("/api/balances/withdraw-all")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["total"], 1)
        self.assertEqual(list(submitted[0]), ["good@example.com"])
        withdraw.assert_called_once_with("good@example.com", "password")
        snapshot = server._withdraw_bulk_snapshot()
        self.assertEqual(snapshot["state"], "done")
        self.assertEqual(snapshot["done"], 1)
        self.assertTrue(snapshot["results"][0]["ok"])
        self.assertNotIn("password", str(snapshot))
        stored = server.load_json(server._withdraw_bulk_path(), {})
        self.assertEqual(stored["state"], "done")
        self.assertEqual(stored["done"], 1)
        self.assertNotIn("password", str(stored))

    def test_cooldown_prevents_duplicate_request(self):
        with patch.object(server.crowtado, "solicitar_link_saque", return_value={"status": "ok"}) as withdraw:
            first, _ = server._withdraw_once("good@example.com", "password")
            second, code = server._withdraw_once("good@example.com", "password")
        self.assertTrue(first["ok"])
        self.assertEqual(code, 429)
        self.assertFalse(second["ok"])
        withdraw.assert_called_once()

    def test_cooldown_survives_service_restart(self):
        with patch.object(server.crowtado, "solicitar_link_saque", return_value={"status": "ok"}) as withdraw:
            first, _ = server._withdraw_once("good@example.com", "password")
            self.assertTrue(first["ok"])
            server._WITHDRAW_LAST_REQUEST.clear()
            server._WITHDRAW_COOLDOWN_LOADED = False
            second, status = server._withdraw_once("good@example.com", "password")
        self.assertEqual(status, 429)
        self.assertFalse(second["ok"])
        withdraw.assert_called_once()

    def test_interrupted_batch_is_preserved_without_retry(self):
        server.save_json(server._withdraw_bulk_path(), {
            "state": "running", "total": 2, "done": 1,
            "current": "second@example.com",
            "results": [{"email": "first@example.com", "ok": True,
                         "message": "link enviado"}],
        })
        server._WITHDRAW_BULK_LOADED = False
        snapshot = server._withdraw_bulk_snapshot()
        self.assertEqual(snapshot["state"], "interrupted")
        self.assertEqual(snapshot["done"], 1)
        self.assertEqual(snapshot["current"], "second@example.com")
        self.assertEqual(len(snapshot["results"]), 1)
        self.assertEqual(server.load_json(server._withdraw_bulk_path(), {})["state"], "interrupted")

    def test_no_external_request_when_cooldown_cannot_be_saved(self):
        with patch.object(server, "save_json", side_effect=OSError("disk full")), \
             patch.object(server.crowtado, "solicitar_link_saque") as withdraw:
            result, status = server._withdraw_once("good@example.com", "password")
        self.assertEqual(status, 503)
        self.assertFalse(result["ok"])
        withdraw.assert_not_called()

    def test_batch_does_not_start_when_history_cannot_be_saved(self):
        with patch.object(server, "_list_accounts", return_value=[{"email": "good@example.com"}]), \
             patch.object(server, "_configured_crowtado_creds", return_value={"good@example.com": "p"}), \
             patch.object(server, "_load_balances", return_value={"good@example.com": {"availableCents": 100}}), \
             patch.object(server.org_policy, "account_kind", return_value="crowtado"), \
             patch.object(server, "_save_withdraw_bulk_locked", side_effect=OSError("disk full")), \
             patch.object(server.crowtado, "solicitar_link_saque") as withdraw:
            response = self.client.post("/api/balances/withdraw-all")
        self.assertEqual(response.status_code, 503)
        withdraw.assert_not_called()

    def test_empty_eligible_set_is_rejected(self):
        with patch.object(server, "_list_accounts", return_value=[{"email": "a@example.com"}]), \
             patch.object(server, "_configured_crowtado_creds", return_value={"a@example.com": "p"}), \
             patch.object(server, "_load_balances", return_value={"a@example.com": {"availableCents": 0}}), \
             patch.object(server.org_policy, "account_kind", return_value="crowtado"):
            response = self.client.post("/api/balances/withdraw-all")
        self.assertEqual(response.status_code, 400)

    def test_running_batch_cannot_be_started_twice(self):
        with server._WITHDRAW_BULK_LOCK:
            server._WITHDRAW_BULK_STATE["state"] = "running"
        with patch.object(server, "_list_accounts", return_value=[{"email": "a@example.com"}]), \
             patch.object(server, "_configured_crowtado_creds", return_value={"a@example.com": "p"}), \
             patch.object(server, "_load_balances", return_value={"a@example.com": {"availableCents": 100}}), \
             patch.object(server.org_policy, "account_kind", return_value="crowtado"):
            response = self.client.post("/api/balances/withdraw-all")
        self.assertEqual(response.status_code, 409)

    def test_individual_withdrawal_requires_confirmed_positive_balance(self):
        email = "good@example.com"
        with patch.object(server, "_list_accounts", return_value=[{"email": email}]), \
             patch.object(server, "_configured_crowtado_creds", return_value={email: "p"}), \
             patch.object(server.org_policy, "account_kind", return_value="crowtado"), \
             patch.object(server.crowtado, "solicitar_link_saque") as withdraw:
            for record in ({"availableCents": 0},
                           {"availableCents": 100, "error": "offline"},
                           {"availableCents": 100, "stale": True},
                           {"availableCents": float("nan")}, {}):
                with self.subTest(record=record), \
                     patch.object(server, "_load_balances", return_value={email: record}):
                    response = self.client.post("/api/balances/withdraw", json={"email": email})
                    self.assertEqual(response.status_code, 400)
            withdraw.assert_not_called()

    def test_individual_withdrawal_is_blocked_during_batch(self):
        email = "good@example.com"
        with server._WITHDRAW_BULK_LOCK:
            server._WITHDRAW_BULK_STATE["state"] = "running"
        with patch.object(server, "_list_accounts", return_value=[{"email": email}]), \
             patch.object(server, "_configured_crowtado_creds", return_value={email: "p"}), \
             patch.object(server.org_policy, "account_kind", return_value="crowtado"), \
             patch.object(server, "_load_balances", return_value={email: {"availableCents": 100}}), \
             patch.object(server.crowtado, "solicitar_link_saque") as withdraw:
            response = self.client.post("/api/balances/withdraw", json={"email": email})
        self.assertEqual(response.status_code, 409)
        withdraw.assert_not_called()

    def test_individual_withdrawal_accepts_confirmed_positive_balance(self):
        email = "good@example.com"
        with patch.object(server, "_list_accounts", return_value=[{"email": email}]), \
             patch.object(server, "_configured_crowtado_creds", return_value={email: "p"}), \
             patch.object(server.org_policy, "account_kind", return_value="crowtado"), \
             patch.object(server, "_load_balances", return_value={email: {"availableCents": 100}}), \
             patch.object(server.crowtado, "solicitar_link_saque", return_value={"status": "ok"}) as withdraw:
            response = self.client.post("/api/balances/withdraw", json={"email": email})
        self.assertEqual(response.status_code, 200)
        withdraw.assert_called_once_with(email, "p")
