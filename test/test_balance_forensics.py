import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from moneymin import crowtado
from moneymin.web import runner, server
from moneymin.web.account_issues import account_issue


SUMMARY = {"availableCents": 2600, "pendingCents": 20,
           "inTransitCents": 0, "lifetimeCents": 2620}


class BalanceForensicsTests(unittest.TestCase):
    def test_refresh_rejects_malformed_email_selection(self):
        client = server.create_app().test_client()
        for body in (["unexpected"], {"emails": "a@example.com"}, {"emails": [None]}):
            with self.subTest(body=body), patch.object(server.BALANCES_RUNNER, "start") as start:
                response = client.post("/api/balances/refresh", json=body)
            self.assertEqual(response.status_code, 400)
            start.assert_not_called()

    def tearDown(self):
        crowtado.clear_cached_session()

    def test_live_identifier_not_found_is_classified_without_browser_retry(self):
        error = crowtado._remote_error("Login", 422, {
            "errors": [{"code": "form_identifier_not_found", "long_message": "private"}]})
        self.assertEqual(error.account_issue_code, "crowtado_account_missing")
        self.assertFalse(crowtado.can_use_browser_fallback(error))
        issue = account_issue("a@example.com", error, stage="Consulta de saldo Crowtado")
        self.assertEqual(issue["code"], "crowtado_account_missing")
        self.assertNotIn("private", str(issue))

    def test_session_renews_exact_session_instead_of_reusing_last_active_token(self):
        session = crowtado.CrowtadoSession(Mock(), "session-a")
        with patch.object(session, "_fapi", return_value=(200, {"jwt": "fresh"})) as call:
            self.assertEqual(session.session_jwt(), "fresh")
        call.assert_called_once_with("/v1/client/sessions/session-a/tokens", {})

    def test_empty_form_is_still_post(self):
        response = Mock(status=200)
        response.read.return_value = b'{"jwt":"fresh"}'
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        session = crowtado.CrowtadoSession(opener, "a")
        session._fapi("/v1/client/sessions/a/tokens", {})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.data, b"")

    def test_read_reauthenticates_once_after_expiry(self):
        expired = crowtado.CrowtadoError("expired", code="authentication")
        with patch.object(crowtado, "_cached_login", side_effect=["old", "new"]) as login, \
             patch.object(crowtado, "_site_trpc", side_effect=[expired, SUMMARY]) as query:
            self.assertEqual(crowtado.consultar_saldo_api("a", "pw"), SUMMARY)
        self.assertEqual(login.call_count, 2)
        self.assertTrue(all(c.kwargs["method"] == "GET" for c in query.call_args_list))

    def test_network_error_does_not_discard_cached_login_or_repeat_password(self):
        cached = Mock()
        cached.session_jwt.side_effect = crowtado.CrowtadoError("network", code="network")
        crowtado._SESSION_CACHE["a"] = (crowtado._password_fingerprint("pw"), cached)
        with patch.object(crowtado, "login") as login:
            with self.assertRaises(crowtado.CrowtadoError):
                crowtado._cached_login(" A ", "pw")
        login.assert_not_called()
        self.assertIn("a", crowtado._SESSION_CACHE)

    def test_summary_requires_complete_integer_values(self):
        for invalid in (True, None, 1.5, float("inf"), "2.5"):
            with self.subTest(value=invalid), self.assertRaises(crowtado.CrowtadoError):
                crowtado._summary_from_payload({**SUMMARY, "availableCents": invalid})
        self.assertEqual(crowtado._summary_from_payload({"availableCents": 9999}), {})
        self.assertEqual(crowtado._summary_from_payload(
            [{"result": {"data": {"json": SUMMARY}}}]), SUMMARY)

    def test_rsc_field_order_does_not_change_balance(self):
        text = json.dumps(SUMMARY, sort_keys=True)
        self.assertEqual(crowtado._extrai_summary(text), SUMMARY)
        self.assertEqual(crowtado._extrai_summary(text.replace('"', '\\"')), SUMMARY)
        self.assertEqual(crowtado._extrai_summary('{"availableCents":1}{"pendingCents":2}'), {})

    def test_invalid_partial_result_preserves_previous_balance_and_timestamp(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(server, "BALANCES_PATH", Path(folder) / "balances.json"):
            server._save_balances({"a": {**SUMMARY, "updated_at": "previous"}})
            server._on_balance_result("a", {"availableCents": 99999}, None)
            saved = server._load_balances()["a"]
        self.assertEqual(saved["availableCents"], 2600)
        self.assertEqual(saved["updated_at"], "previous")
        self.assertTrue(saved["stale"])
        self.assertEqual(saved["issue"]["code"], "invalid_response")

    def test_one_corrupted_record_does_not_abort_valid_result(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(server, "BALANCES_PATH", Path(folder) / "balances.json"):
            server._save_balances({"a": "broken"})
            server._on_balance_result("a", SUMMARY, None)
            self.assertFalse(server._load_balances()["a"]["stale"])

    def test_runner_reports_per_account_failures_without_opening_browser(self):
        instance = runner.BalancesRunner()
        instance.state = "running"
        errors = [crowtado.CrowtadoError("missing", code="crowtado_account_missing"),
                  crowtado.CrowtadoError("limit", code="rate_limit")]
        received = []
        with patch.object(crowtado, "consultar_saldo_api", side_effect=errors), \
             patch.object(crowtado, "consultar_saldo_navegador") as browser:
            instance._run({"a": "pw", "b": "pw"}, lambda *args: received.append(args))
        browser.assert_not_called()
        self.assertEqual(instance.snapshot()["failed"], 2)
        self.assertEqual(instance.snapshot()["done"], 2)
        self.assertTrue(all(isinstance(row[2], crowtado.CrowtadoError) for row in received))

    def test_persistence_failure_does_not_skip_remaining_accounts(self):
        instance = runner.BalancesRunner()
        instance.state = "running"
        callback = Mock(side_effect=[OSError("private path"), None])
        with patch.object(crowtado, "consultar_saldo_api", return_value=SUMMARY):
            instance._run({"a": "pw", "b": "pw"}, callback)
        self.assertEqual(callback.call_count, 2)
        self.assertEqual(instance.snapshot()["state"], "error")
        self.assertEqual(instance.snapshot()["failed"], 1)
        self.assertNotIn("private path", instance.snapshot()["error"])

    def test_partial_result_is_counted_as_failure_not_success(self):
        instance = runner.BalancesRunner()
        callback = Mock()
        with patch.object(crowtado, "consultar_saldo_api", return_value={"availableCents": 1}):
            instance._run({"a": "pw"}, callback)
        self.assertEqual(instance.snapshot()["failed"], 1)
        self.assertEqual(instance.snapshot()["fast_done"], 0)
        self.assertIsNone(callback.call_args.args[1])
        self.assertEqual(callback.call_args.args[2].account_issue_code, "invalid_response")


if __name__ == "__main__":
    unittest.main()
