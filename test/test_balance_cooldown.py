import io
import json
import unittest
import urllib.error
from email.message import Message
from unittest.mock import Mock, patch

from moneymin import crowtado, hostinger_mail
from moneymin.web.runner import BalancesRunner
from moneymin.web.account_issues import account_issue


SUMMARY = {"availableCents": 2600, "pendingCents": 20,
           "inTransitCents": 0, "lifetimeCents": 2620}


class BalanceCooldownTests(unittest.TestCase):
    def test_provider_cooldown_retries_same_read_before_next_account(self):
        instance = BalancesRunner()
        callback = Mock()
        limited = crowtado.CrowtadoError("limited", code="rate_limit", retry_after_seconds=17)
        with patch.object(crowtado, "consultar_saldo_api", side_effect=[limited, SUMMARY, SUMMARY]) as query, \
             patch.object(crowtado, "consultar_saldo_navegador") as browser, \
             patch.object(instance._stop, "wait", return_value=False) as wait:
            instance._run({"a": "pw", "b": "pw"}, callback)
        self.assertEqual([c.args[0] for c in query.call_args_list], ["a", "a", "b"])
        self.assertGreater(wait.call_args.args[0], 16)
        self.assertLessEqual(wait.call_args.args[0], 17)
        self.assertEqual(callback.call_count, 2)
        self.assertEqual(instance.snapshot()["failed"], 0)
        self.assertEqual(instance.snapshot()["rate_limit_retries"], 1)
        browser.assert_not_called()

    def test_persistent_limit_does_not_hammer_or_overwrite_remaining_accounts(self):
        instance = BalancesRunner()
        instance.results = {email: {"email": email, "state": "queued"} for email in ("a", "b", "c")}
        callback = Mock()
        limited = crowtado.CrowtadoError("limited", code="rate_limit")
        with patch.object(crowtado, "consultar_saldo_api", side_effect=limited) as query, \
             patch.object(instance._stop, "wait", return_value=False):
            instance._run({"a": "pw", "b": "pw", "c": "pw"}, callback)
        self.assertEqual(query.call_count, 3)
        self.assertTrue(all(c.args[0] == "a" for c in query.call_args_list))
        self.assertEqual(callback.call_count, 1)
        state = instance.snapshot()
        self.assertEqual(state["state"], "error")
        self.assertEqual(state["done"], 1)
        self.assertEqual([r["state"] for r in state["results"]], ["error", "not_consulted", "not_consulted"])
        self.assertGreater(state["retry_at"], 0)

    def test_stop_interrupts_cooldown_without_another_read_or_save(self):
        instance = BalancesRunner()
        instance.results = {email: {"email": email, "state": "queued"} for email in ("a", "b")}
        callback = Mock()
        def stop_wait(delay):
            self.assertEqual(instance.snapshot()["phase"], "cooldown")
            instance._stop.set()
            return True
        with patch.object(crowtado, "consultar_saldo_api", side_effect=crowtado.CrowtadoError("limit", code="rate_limit")) as query, \
             patch.object(instance._stop, "wait", side_effect=stop_wait):
            instance._run({"a": "pw", "b": "pw"}, callback)
        self.assertEqual(query.call_count, 1)
        callback.assert_not_called()
        self.assertEqual(instance.snapshot()["state"], "stopped")
        self.assertTrue(all(r["state"] == "not_consulted" for r in instance.snapshot()["results"]))

    def test_new_batch_honors_unexpired_cooldown(self):
        instance = BalancesRunner()
        instance.retry_at = 115
        with patch("moneymin.web.runner.time.time", return_value=100), \
             patch.object(crowtado, "consultar_saldo_api", return_value=SUMMARY) as query, \
             patch.object(crowtado, "consultar_saldo_navegador") as browser, \
             patch.object(instance._stop, "wait", return_value=False) as wait:
            instance._run({"a": "pw"}, Mock())
        wait.assert_called_once_with(15)
        query.assert_called_once_with("a", "pw")
        browser.assert_not_called()
        self.assertEqual(instance.snapshot()["failed"], 0)
        self.assertEqual(instance.snapshot()["results"][0]["state"], "confirmed")

    def test_stop_before_request_boundary_does_not_begin_another_read(self):
        instance = BalancesRunner()
        instance.results = {"a": {"email": "a", "state": "queued"}}
        callback = Mock()
        original = instance._begin_api_attempt
        def request_boundary(*args):
            instance._stop.set()
            return original(*args)
        with patch.object(instance, "_begin_api_attempt", side_effect=request_boundary), \
             patch.object(crowtado, "consultar_saldo_api") as query:
            instance._run({"a": "pw"}, callback)
        query.assert_not_called()
        callback.assert_not_called()
        self.assertEqual(instance.snapshot()["state"], "stopped")

    def test_authentication_header_cooldown_is_retained_without_remote_body(self):
        headers = Message()
        headers["Retry-After"] = "45"
        error = urllib.error.HTTPError("https://clerk.crowtado.com", 429, "limited", headers,
            io.BytesIO(json.dumps({"errors": [{"code": "too_many_requests", "message": "private"}]}).encode()))
        session = crowtado.CrowtadoSession(Mock(open=Mock(side_effect=error)), "")
        with self.assertRaises(crowtado.CrowtadoError) as raised:
            session._fapi("/v1/client/sign_ins", {})
        self.assertEqual(raised.exception.retry_after_seconds, 45)
        self.assertEqual(raised.exception.account_issue_code, "rate_limit")
        self.assertNotIn("private", str(raised.exception))

    def test_retry_after_date_and_invalid_header(self):
        with patch.object(crowtado.time, "time", return_value=0):
            self.assertEqual(crowtado._retry_after_seconds({"Retry-After": "Thu, 01 Jan 1970 00:01:00 GMT"}), 60)
        for raw in ["bad", "-9", "nan"]:
            self.assertIsNone(crowtado._retry_after_seconds({"Retry-After": raw}))

    def test_hostinger_unauthorized_is_not_crowtado_password_or_browser_fallback(self):
        profile = {"id": "mail", "token": "fixture-token", "mailbox_id": "box", "routes": ["example.invalid"]}
        with patch.object(hostinger_mail, "configured_connections", return_value=[profile]), \
             patch.object(hostinger_mail, "_request", return_value=(401, {"private": "secret"})):
            with self.assertRaises(hostinger_mail.MailAuthenticationError):
                hostinger_mail.max_uid("a@example.invalid")
            with self.assertRaises(hostinger_mail.MailAuthenticationError):
                hostinger_mail.wait_for_code("a@example.invalid", poll=0)
        error = crowtado.CrowtadoError("token secret", code="mail_authentication")
        self.assertFalse(crowtado.can_use_browser_fallback(error))
        issue = account_issue("a@example.invalid", error, stage="Consulta de saldo Crowtado")
        self.assertEqual(issue["code"], "mail_authentication")
        self.assertIn("Hostinger", issue["action"])
        self.assertNotIn("secret", str(issue))

    def test_bad_mail_token_stops_before_requesting_a_new_code(self):
        session = Mock()
        session._fapi.side_effect = [(200, {}), (200, {"response": {
            "status": "needs_second_factor", "id": "challenge", "supported_second_factors": [{"strategy": "email_code"}]}})]
        with patch.object(crowtado, "CrowtadoSession", return_value=session), \
             patch.object(hostinger_mail, "max_uid", side_effect=hostinger_mail.MailAuthenticationError("expired")):
            with self.assertRaises(crowtado.CrowtadoError) as raised:
                crowtado.login("a@example.invalid", "fixture")
        self.assertEqual(raised.exception.account_issue_code, "mail_authentication")
        self.assertEqual(session._fapi.call_count, 2)


if __name__ == "__main__":
    unittest.main()
