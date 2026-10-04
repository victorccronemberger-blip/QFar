import json
import unittest
from unittest.mock import patch

from moneymin.minute_api import AuthError, Session, HttpResponse, _auth_failure
from moneymin.web.account_issues import account_issue


class AccessResponseDiagnosticsTests(unittest.TestCase):
    def test_profile_verification_preserves_block_header_without_inventing_disabled(self):
        session = Session({'email': 'fixture@example.invalid', "idToken": "fake"})
        session._live = True
        reply = HttpResponse(403, '{"detail":"Forbidden"}',
                             {"x-blocked-reason": "user", "Authorization": "secret-token"})
        with patch.object(session, "request_detailed", return_value=reply) as request:
            with self.assertRaises(AuthError) as caught:
                session.ensure_auth()
        request.assert_called_once_with("GET", "/api/v1/users/me")
        issue = account_issue("a@example.com", caught.exception)
        self.assertEqual(issue["code"], "forbidden")
        self.assertFalse(issue["restriction_confirmed"])
        self.assertIn("X-Blocked-Reason: user", issue["detail"])
        self.assertIn("GET /api/v1/users/me", issue["detail"])
        self.assertNotIn("secret-token", str(issue))

    def test_explicit_app_check_failure_is_not_a_password_or_disabled_error(self):
        for status in (401, 403):
            error = _auth_failure(status, '{"detail":{"error":"appcheck_required"}}', "Profile")
            issue = account_issue("a@example.com", error)
            self.assertEqual(issue["code"], "app_check")
            self.assertFalse(issue["restriction_confirmed"])

    def test_nested_disabled_message_is_recognized_exactly(self):
        for message, code in (("User account is disabled.", "restricted"),
                              ("User account may be disabled.", "forbidden")):
            error = _auth_failure(403, json.dumps({"detail": {"message": message}}), "Profile")
            self.assertEqual(error.account_issue_code, code)

    def test_only_documented_header_values_enter_diagnostics(self):
        for value in ("secret-token", "https://example.com/private", "user@example.com"):
            issue = account_issue("a@example.com", _auth_failure(
                403, "Forbidden", "Profile", headers={"X-Blocked-Reason": value}))
            self.assertNotIn(value, issue["detail"])
            self.assertFalse(issue["restriction_confirmed"])

    def test_device_header_is_preserved_but_does_not_confirm_account_restriction(self):
        issue = account_issue("a@example.com", _auth_failure(
            403, "Forbidden", "Profile", headers={"X-Blocked-Reason": "device"}))
        self.assertEqual(issue["code"], "device")
        self.assertFalse(issue["restriction_confirmed"])
