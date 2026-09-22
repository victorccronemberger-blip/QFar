from __future__ import annotations

import unittest
from unittest.mock import patch

from moneymin.web import server


class BulkWithdrawalTests(unittest.TestCase):
    def setUp(self):
        self.client = server.create_app().test_client()
        self._previous_balance_state = server.BALANCES_RUNNER.state
        server.BALANCES_RUNNER.state = "idle"
        server._WITHDRAW_LAST_REQUEST.clear()
        server._WITHDRAW_IN_FLIGHT.clear()
        with server._WITHDRAW_BULK_LOCK:
            server._WITHDRAW_BULK_STATE.update(state="idle", total=0, done=0, results=[])

    def tearDown(self):
        server.BALANCES_RUNNER.state = self._previous_balance_state
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

    def test_cooldown_prevents_duplicate_request(self):
        with patch.object(server.crowtado, "solicitar_link_saque", return_value={"status": "ok"}) as withdraw:
            first, _ = server._withdraw_once("good@example.com", "password")
            second, code = server._withdraw_once("good@example.com", "password")
        self.assertTrue(first["ok"])
        self.assertEqual(code, 429)
        self.assertFalse(second["ok"])
        withdraw.assert_called_once()

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
