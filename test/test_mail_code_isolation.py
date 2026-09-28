import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch, Mock

from moneymin import hostinger_mail as mail, crowtado


class MailCodeIsolationTests(unittest.TestCase):
    def setUp(self):
        self.profiles = [dict(id="box", token="fixture", mailbox_id="box", routes=[])]
        p = patch.object(mail.config, "HOSTINGER_MAIL_PROFILES", self.profiles)
        p.start()
        self.addCleanup(p.stop)

    def message(self, uid=11, target="person@example.com", code="123456"):
        return dict(uid=uid, to=[dict(address=target)], subject=f"{code} verification code")

    def wait(self, **kwargs):
        return mail.wait_for_code("person@example.com", sender="crowtado.com",
                                  min_uid={"box": 10}, timeout=2, poll=0, **kwargs)

    def test_wrong_missing_recipient_and_old_code_are_ignored(self):
        messages = [self.message(target="other@example.com"),
                    dict(uid=12, subject="654321 verification code"),
                    self.message(uid=9), self.message(uid=13, code="987654")]
        with patch.object(mail, "search_messages", return_value=messages), \
             patch.object(mail, "delete_message") as delete:
            self.assertEqual(self.wait(), "987654")
        delete.assert_not_called()

    def test_busy_shared_inbox_reads_next_page(self):
        others = [self.message(uid=1000-i, target="other@example.com") for i in range(100)]
        with patch.object(mail, "search_messages", side_effect=[others, [self.message()]]) as search:
            self.assertEqual(self.wait(), "123456")
        self.assertEqual([c.kwargs["page"] for c in search.call_args_list], [1, 2])

    def test_transient_failure_and_delayed_delivery_recover(self):
        with patch.object(mail, "search_messages", side_effect=[
                mail.TemporaryMailError("HTTP 429"), [], [self.message()]]):
            self.assertEqual(self.wait(), "123456")

    def test_failed_snapshot_box_cannot_supply_old_code(self):
        self.profiles.append(dict(id="unread", token="other", routes=[]))
        with patch.object(mail, "search_messages", return_value=[self.message()]) as search:
            self.assertEqual(self.wait(), "123456")
        self.assertEqual(search.call_count, 1)
        self.assertEqual(search.call_args.kwargs["token"], "fixture")

    def test_snapshot_always_identifies_the_mailbox(self):
        with patch.object(mail, "search_messages", return_value=[self.message()]):
            self.assertEqual(mail.max_uid("person@example.com"), {"box": 11})

    def test_snapshot_retries_a_temporary_outage(self):
        with patch.object(mail, "search_messages", side_effect=[
                mail.TemporaryMailError("HTTP 503"), [self.message()]]), \
             patch.object(mail.time, "sleep"):
            self.assertEqual(mail.max_uid("person@example.com"), {"box": 11})

    def test_http_rate_limit_is_retryable(self):
        with patch.object(mail, "_request", return_value=(429, {})):
            with self.assertRaises(mail.TemporaryMailError):
                mail.search_messages(token="fixture", mailbox="box")

    def test_missing_cursor_never_falls_back_to_zero(self):
        with patch.object(mail, "search_messages") as search:
            with self.assertRaises(mail.MailError):
                mail.wait_for_code("person@example.com", min_uid={"removed": 10})
        search.assert_not_called()

    def test_permanent_failure_does_not_spin(self):
        with patch.object(mail, "search_messages", side_effect=mail.MailError("HTTP 401")) as search:
            with self.assertRaises(mail.MailError):
                self.wait()
        self.assertEqual(search.call_count, 1)

    def test_parallel_same_account_reuses_single_login(self):
        import time
        crowtado.clear_cached_session()
        self.addCleanup(crowtado.clear_cached_session)
        session = Mock()
        def login(*args):
            time.sleep(.03)
            return session
        with patch.object(crowtado, "login", side_effect=login) as authenticate:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: crowtado._cached_login("person@example.com", "fixture"), range(4)))
        self.assertTrue(all(s is session for s in results))
        authenticate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
