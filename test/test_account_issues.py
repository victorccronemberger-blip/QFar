import json
import unittest
from contextlib import ExitStack
from unittest import mock

from moneymin.minute_api import AuthError
from moneymin.web import server
from moneymin.web.account_issues import account_issue


class AccountIssueTests(unittest.TestCase):
    def test_causes_have_distinct_actions(self):
        cases = [
            (AuthError("HTTP 401 INVALID_PASSWORD"), "authentication"),
            (AuthError("conta desativada no HUB"), "restricted"),
            (AuthError("sem token salvo"), "missing_access"),
            (RuntimeError("a conta não pertence a nenhuma organização"), "organization"),
            (RuntimeError("HTTP 429"), "rate_limit"),
            (RuntimeError("HTTP 503"), "service"),
            (RuntimeError("HTTP 403"), "forbidden"),
            (TimeoutError("read timed out"), "timeout"),
            (OSError("getaddrinfo failed"), "network"),
            (OSError("CERTIFICATE_VERIFY_FAILED"), "tls"),
            (RuntimeError("unexpected response"), "unknown"),
        ]
        for error, code in cases:
            with self.subTest(code=code):
                result = account_issue("test@example.com", error)
                self.assertEqual(result["code"], code)
                self.assertTrue(result["reason"])
                self.assertTrue(result["action"])

    def test_exception_payloads_never_leak_to_diagnostic(self):
        result = account_issue("test@example.com", AuthError(
            'HTTP 401 password="private-password" refreshToken="private-token" '
            'Bearer secret-jwt https://example.invalid/?sig=private-signature'))
        serialized = json.dumps(result)
        for secret in ("private-password", "private-token", "secret-jwt", "private-signature"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(result["detail"], "AuthError · HTTP 401")


class AccountDiagnosticEndpointsTests(unittest.TestCase):
    def setUp(self):
        self.client = server.create_app().test_client()

    def preflight(self, accounts, known, resolve):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(server, "RUNNER", mock.Mock(running=False)))
            stack.enter_context(mock.patch.object(server, "HOLO_CACHE_RUNNER", mock.Mock(running=False)))
            stack.enter_context(mock.patch.object(server, "_list_accounts", return_value=[
                {"email": email} for email in known]))
            stack.enter_context(mock.patch.object(server, "_resolve_org", side_effect=resolve))
            stack.enter_context(mock.patch.object(server.campaign, "available_tasks", return_value=[
                {"id": "task", "name": "task", "clip_count": 1, "available_for_duration": True}]))
            stack.enter_context(mock.patch.object(server.readiness, "campaign_readiness", return_value={
                "ready": True, "checks": []}))
            stack.enter_context(mock.patch.object(server, "_storage_snapshot", return_value={
                "free_bytes": 20 * 1024 ** 3}))
            response = self.client.post("/api/campaigns/preflight", json={
                "accounts": accounts, "tasks": [{"task_id": "task"}], "dataset": "ego4d"})
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def test_failed_account_is_named_with_reason_and_action(self):
        def resolve(email):
            if email == "bad@example.com":
                raise TimeoutError("read timed out password=secret")
            return "org"
        result = self.preflight(["good@example.com", "bad@example.com"],
                                ["good@example.com", "bad@example.com"], resolve)
        self.assertFalse(result["ok"])
        self.assertEqual(result["accounts"]["validated"], 1)
        self.assertEqual(result["account_issues"][0]["email"], "bad@example.com")
        self.assertEqual(result["account_issues"][0]["code"], "timeout")
        self.assertIn("bad@example.com", " ".join(result["blockers"]))
        self.assertNotIn("secret", json.dumps(result))

    def test_missing_account_does_not_hide_other_accounts_diagnostics(self):
        result = self.preflight(["missing@example.com", "good@example.com"],
                                ["good@example.com"], lambda _: "org")
        self.assertEqual(result["accounts"]["validated"], 1)
        self.assertEqual(result["account_issues"][0]["code"], "missing_access")
        self.assertFalse(result["ok"])

    def test_all_auth_failures_do_not_invent_missing_categories(self):
        result = self.preflight(["bad@example.com"], ["bad@example.com"],
                                mock.Mock(side_effect=AuthError("HTTP 401")))
        self.assertEqual(len(result["blockers"]), 1)
        self.assertIn("bad@example.com", result["blockers"][0])

    def test_valid_access_passes_without_issues(self):
        result = self.preflight(["good@example.com"], ["good@example.com"], lambda _: "org")
        self.assertTrue(result["ok"])
        self.assertEqual(result["account_issues"], [])

    def test_individual_and_batch_checks_return_same_issue(self):
        with mock.patch.object(server.Session, "from_email", side_effect=AuthError("HTTP 401")), \
             mock.patch.object(server, "_list_accounts", return_value=[{"email": "bad@example.com"}]), \
             mock.patch.object(server, "RUNNER", mock.Mock(running=False)):
            single = self.client.post("/api/accounts/bad@example.com/check")
            batch = self.client.post("/api/accounts/check-all")
        self.assertEqual(single.status_code, 400)
        self.assertEqual(single.get_json()["issue"], batch.get_json()["errors"][0]["issue"])


if __name__ == "__main__":
    unittest.main()
