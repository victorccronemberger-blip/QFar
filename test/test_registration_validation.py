import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from moneymin import minute_api, tls
from moneymin.web import server


class RegistrationValidationTests(unittest.TestCase):
    def setUp(self):
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.identity = {"birth_month": 1, "birth_year": 1990, "gender": "male"}
        self.context.enter_context(patch.object(server.account_bans, "require_not_banned"))
        self.context.enter_context(patch.object(server, "_set_account_removed"))
        self.context.enter_context(patch.object(server, "_save_crowtado_cred"))
        self.signup = self.context.enter_context(patch.object(server.crowtado, "criar_conta"))
        self.crowtado_login = self.context.enter_context(patch.object(server.crowtado, "login"))
        self.demographics = self.context.enter_context(patch.object(server.crowtado, "preencher_demografia"))
        self.link = self.context.enter_context(patch.object(server.crowtado, "vincular_minute"))
        self.register = self.context.enter_context(patch.object(minute_api, "register"))
        self.login = self.context.enter_context(patch.object(minute_api, "login"))
        self.session = Mock()
        self.session.ensure_auth.return_value = {"organizations": [
            {"resourceKey": server.config.ORG_KEY, "disabled": False}]}
        self.context.enter_context(patch.object(server.Session, "from_email", return_value=self.session))

    def run_registration(self):
        return server._full_register_account("review@example.invalid", "test-only", self.identity)

    def test_existing_crowtado_account_preserves_demographics(self):
        self.signup.side_effect = RuntimeError("account already exists")
        result = self.run_registration()
        self.assertIsNone(result["error"])
        self.assertEqual(result["steps"]["demographics"]["status"], "skip")
        self.demographics.assert_not_called()

    def test_new_account_populates_demographics(self):
        self.assertIsNone(self.run_registration()["error"])
        self.demographics.assert_called_once()

    def test_unrelated_signup_errors_do_not_trigger_existing_account_login(self):
        for message in ("Executable doesn't exist", "browser already closed",
                        "request has taken too long"):
            with self.subTest(message=message), \
                 patch.object(server.time, "sleep"):
                self.signup.side_effect = RuntimeError(message)
                result = self.run_registration()
                self.assertEqual(result["steps"]["crowtado_signup"]["status"], "fail")
                self.crowtado_login.assert_not_called()
                self.register.assert_not_called()

    def test_explicit_clerk_duplicate_can_resume(self):
        for message in ("form_identifier_exists", "This email address is taken.",
                        "Este endereço de e-mail já está em uso."):
            with self.subTest(message=message):
                self.signup.side_effect = RuntimeError(message)
                self.assertIsNone(self.run_registration()["error"])
                self.assertEqual(self.crowtado_login.call_args.args,
                                 ("review@example.invalid", "test-only"))

    def test_partial_account_recovers_only_after_explicit_demographics_gate(self):
        self.signup.side_effect = RuntimeError("account already exists")
        self.link.side_effect = [RuntimeError("412 DEMOGRAPHICS_REQUIRED"), None]
        result = self.run_registration()
        self.assertIsNone(result["error"])
        self.demographics.assert_called_once()
        self.assertEqual(self.link.call_count, 2)
        self.assertEqual(result["steps"]["demographics"]["status"], "ok")

    def test_other_link_failure_does_not_modify_demographics(self):
        self.signup.side_effect = RuntimeError("account already exists")
        self.link.side_effect = RuntimeError("HTTP 503 unavailable")
        self.assertIsNotNone(self.run_registration()["error"])
        self.demographics.assert_not_called()

    def test_save_failure_returns_partial_steps_and_stops_external_work(self):
        with patch.object(server, "_save_crowtado_cred", side_effect=OSError("sensitive path")):
            result = self.run_registration()
        self.assertTrue(result["partial"])
        self.assertEqual(result["steps"]["save_partial"]["status"], "fail")
        self.assertNotIn("sensitive path", str(result))
        self.demographics.assert_not_called()
        self.register.assert_not_called()

    def test_manual_save_failure_returns_json_and_completed_steps(self):
        with patch.object(server, "_save_crowtado_cred", side_effect=OSError("disk failure")), \
             patch.object(server, "_hostinger_is_configured", return_value=True), \
             patch.object(server, "ORG_MIGRATION", Mock(running=False)), \
             patch.object(server, "_BULK_REGISTER_STATE", {"state": "idle"}):
            response = server.create_app().test_client().post("/api/accounts/register", json={
                "email": "review@example.invalid", "password": "test-only"})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.is_json)
        self.assertEqual(response.get_json()["steps"]["crowtado_signup"]["status"], "ok")
        self.assertEqual(response.get_json()["steps"]["save_partial"]["status"], "fail")

    def test_invalid_invite_is_not_treated_as_duplicate(self):
        self.register.side_effect = RuntimeError("registro falhou (400): invalid invite code")
        result = self.run_registration()
        self.assertEqual(result["steps"]["minute_register"]["status"], "fail")
        self.login.assert_not_called()
        self.link.assert_not_called()

    def test_explicit_duplicate_requires_membership(self):
        self.register.side_effect = RuntimeError("EMAIL_EXISTS")
        self.session.ensure_auth.return_value = {"organizations": []}
        result = self.run_registration()
        self.assertIn("Organização", result["error"])
        self.login.assert_called_once()
        self.link.assert_not_called()

    def test_duplicate_with_confirmed_membership_can_resume(self):
        self.register.side_effect = RuntimeError("EMAIL_EXISTS")
        result = self.run_registration()
        self.assertIsNone(result["error"])
        self.assertEqual(result["steps"]["minute_register"]["status"], "skip")

    def test_registration_success_with_suspended_membership_is_rejected(self):
        self.session.ensure_auth.return_value["organizations"][0]["disabled"] = True
        self.assertIn("suspensa", self.run_registration()["error"])
        self.link.assert_not_called()


class RegistrationPreflightTests(unittest.TestCase):
    def test_selected_domain_checks_its_profile_only(self):
        profiles = [{"id": "bad", "token": "invalid-test", "routes": ["bad.invalid"]},
                    {"id": "good", "token": "valid-test", "routes": ["good.invalid"]}]
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(server.config, "HOSTINGER_MAIL_PROFILES", profiles), \
             patch.object(server.crowtado, "_chrome_exe", return_value="chrome"), \
             patch.object(tls, "urlopen", return_value=response), \
             patch.object(server.hostinger_mail, "test_connection") as check:
            def test_connection(**kwargs):
                if kwargs["token"] == "invalid-test":
                    raise RuntimeError("invalid profile")
                return {"mailboxes": 1}
            check.side_effect = test_connection
            self.assertTrue(server._preflight_checks("good.invalid")["ready"])
            check.assert_called_once_with(token="valid-test", mailbox=None)
            self.assertFalse(server._preflight_checks("bad.invalid")["ready"])
            check.reset_mock()
            self.assertFalse(server._preflight_checks("unknown.invalid")["ready"])
            check.assert_not_called()
            profiles[0]["routes"] = ["good.invalid"]
            self.assertTrue(server._preflight_checks("good.invalid")["ready"])
            self.assertEqual(check.call_count, 2)
