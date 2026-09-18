import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from moneymin import config, minute_api
from moneymin.minute_api import AuthError, Session
from moneymin.web import server
from moneymin.web.account_issues import account_issue


class HealthResilienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for patch in (
            mock.patch.object(server, "ACCOUNT_HEALTH_PATH", Path(self.tmp.name) / "health.json"),
            mock.patch.object(server, "BALANCES_PATH", Path(self.tmp.name) / "balances.json"),
            mock.patch.object(server, "PREFS_PATH", Path(self.tmp.name) / "prefs.json"),
            mock.patch.object(server.time, "sleep"),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.profile = {"organizations": [{"resourceKey": config.ORG_KEY}], "disabled": False}

    def test_keywords_in_payload_do_not_confirm_a_restriction(self):
        for message in ('HTTP 503 {"disabled": false}', 'HTTP 403 inactive',
                        'conta desativada no HUB', 'timeout token disabled', 'senha mencionada em resposta inesperada'):
            with self.subTest(message=message):
                issue = account_issue("a@example.com", AuthError(message))
                self.assertNotEqual(issue["code"], "restricted")
                self.assertFalse(issue["restriction_confirmed"])

    def test_temporary_failure_recovers_without_migration_or_removal(self):
        sess = mock.Mock(data={"expires_at": 123})
        sess.ensure_auth.side_effect = [AuthError("HTTP 503", code="service"), self.profile]
        with mock.patch.object(server.Session, "from_email", return_value=sess), \
             mock.patch.object(server, "_set_account_removed") as remove:
            row = server._check_account_health("a@example.com")
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(sess.ensure_auth.call_count, 2)
        sess.join_org.assert_not_called()
        remove.assert_not_called()

    def test_failed_check_preserves_last_success_without_claiming_current_access(self):
        previous = {"status": "active", "checked_at": "2026-09-17T10:00:00Z"}
        server._save_account_check("a@example.com", previous)
        with mock.patch.object(server.Session, "from_email", side_effect=TimeoutError("network timeout")):
            row = server._check_account_health("a@example.com")
        self.assertEqual(row["status"], "inconclusive")
        self.assertEqual(row["last_success_at"], "2026-09-17T10:00:00Z")
        saved = json.loads(server.ACCOUNT_HEALTH_PATH.read_text())
        self.assertEqual(saved["a@example.com"]["status"], "inconclusive")

    def test_missing_org_is_not_a_dead_account_and_check_never_joins(self):
        sess = mock.Mock(data={})
        sess.ensure_auth.return_value = {"organizations": [{"resourceKey": config.HUB_ORG_KEY}]}
        with mock.patch.object(server.Session, "from_email", return_value=sess):
            row = server._check_account_health("a@example.com")
        self.assertEqual(row["status"], "needs_org")
        self.assertFalse(row["issue"]["restriction_confirmed"])
        sess.join_org.assert_not_called()

    def test_restriction_requires_explicit_evidence_and_is_not_retried(self):
        with mock.patch.object(server.Session, "from_email", side_effect=AuthError("disabled", code="restricted")) as auth:
            row = server._check_account_health("a@example.com")
        self.assertEqual(row["status"], "disabled")
        self.assertTrue(row["issue"]["restriction_confirmed"])
        auth.assert_called_once()

    def test_target_org_disabled_is_confirmed_but_unrelated_org_does_not_block(self):
        sess = mock.Mock(data={})
        for target_disabled in (False, True):
            sess.ensure_auth.return_value = {"organizations": [
                {"resourceKey": config.HUB_ORG_KEY, "disabled": True},
                {"resourceKey": config.ORG_KEY, "disabled": target_disabled}]}
            with mock.patch.object(server.Session, "from_email", return_value=sess):
                row = server._check_account_health("a@example.com")
            self.assertEqual(row["status"], "disabled" if target_disabled else "active")

    def test_balance_failure_keeps_last_known_values_and_timestamp(self):
        server._save_balances({"a@example.com": {
            "availableCents": 1200, "pendingCents": 450, "updated_at": "old-success"}})
        server._on_balance_result("a@example.com", None, "HTTP 503 private-token")
        row = server._load_balances()["a@example.com"]
        self.assertEqual(row["availableCents"], 1200)
        self.assertEqual(row["pendingCents"], 450)
        self.assertEqual(row["updated_at"], "old-success")
        self.assertTrue(row["stale"])
        self.assertNotIn("private-token", json.dumps(row))
        server._on_balance_result("a@example.com", {"availableCents": 0, "pendingCents": 0}, None)
        row = server._load_balances()["a@example.com"]
        self.assertFalse(row["stale"])
        self.assertIsNone(row["error"])
        self.assertEqual(row["availableCents"], 0)


class AuthEvidenceTests(unittest.TestCase):
    def test_malformed_refresh_does_not_partially_replace_saved_credentials(self):
        token = {"idToken": "old-id", "refreshToken": "old-refresh"}
        for payload in ({"id_token": "new-id"},
                        {"id_token": "new-id", "refresh_token": "new-refresh", "expires_in": "bad"}):
            with mock.patch.object(minute_api, "_request", return_value=(200, json.dumps(payload))):
                with self.assertRaises(AuthError) as caught:
                    minute_api._refresh(token)
                self.assertEqual(caught.exception.account_issue_code, "invalid_response")
                self.assertEqual(token, {"idToken": "old-id", "refreshToken": "old-refresh"})

    def test_refresh_outage_does_not_attempt_password_login(self):
        sess = Session({"idToken": "fake", "refreshToken": "fake"})
        with mock.patch.object(minute_api, "_refresh", side_effect=AuthError("HTTP 503", code="service")), \
             mock.patch.object(sess, "_relogin") as login:
            with self.assertRaises(AuthError) as caught:
                sess.refresh()
        self.assertEqual(caught.exception.account_issue_code, "service")
        login.assert_not_called()

    def test_retry_after_401_preserves_refresh_network_failure(self):
        sess = Session({"idToken": "fake", "expires_at": time.time() + 9999})
        sess._live = True
        with mock.patch.object(minute_api, "_request", return_value=(401, "unauthorized")), \
             mock.patch.object(sess, "refresh", side_effect=AuthError("HTTP 503", code="service")):
            with self.assertRaises(AuthError) as caught:
                sess.request("GET", "/api/v1/users/me")
        self.assertEqual(caught.exception.account_issue_code, "service")

    def test_profile_errors_are_classified_by_response_not_generic_login_advice(self):
        sess = Session({"idToken": "fake"})
        sess._live = True
        for status, body, code in ((503, "disabled", "service"), (403, "inactive", "forbidden"),
                                    (-1, "timeout", "timeout"), (429, "rate limit", "rate_limit"),
                                    (200, "[]", "invalid_response"), (200, "{}", "invalid_response")):
            with self.subTest(status=status, body=body), \
                 mock.patch.object(sess, "request", return_value=(status, body)):
                with self.assertRaises(AuthError) as caught:
                    sess.ensure_auth()
                self.assertEqual(caught.exception.account_issue_code, code)

    def test_only_explicit_disabled_field_confirms_profile_restriction(self):
        sess = Session({"idToken": "fake"})
        sess._live = True
        with mock.patch.object(sess, "request", return_value=(200, json.dumps({"disabled": True, "organizations": []}))), \
             mock.patch.object(sess, "version_gate", return_value=None):
            with self.assertRaises(AuthError) as caught:
                sess.ensure_auth()
        self.assertEqual(caught.exception.account_issue_code, "restricted")

    def test_firebase_restriction_requires_exact_error_code(self):
        for body, expected in ((json.dumps({"error": {"message": "USER_DISABLED"}}), "restricted"),
                               (json.dumps({"error": {"message": "TOKEN_EXPIRED"}}), "authentication"),
                               ("USER_DISABLED", "unknown")):
            error = minute_api._auth_failure(400, body, "refresh", firebase=True)
            self.assertEqual(error.account_issue_code, expected)
