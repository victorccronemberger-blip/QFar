import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from moneymin.web import server


class BulkRegisterTests(unittest.TestCase):
    def setUp(self):
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.profiles = [{"id": "test", "name": "Test", "token": "test-only",
                          "routes": ["@Example.invalid", "person@other.invalid"]}]
        self.identity = {"email": "review@example.invalid", "senha": "test-only",
                         "nome": "Review", "sobrenome": "Test", "gender": "male",
                         "birth_month": 1, "birth_year": 1990}
        self.context.enter_context(patch.object(server, "_BULK_REGISTER_STATE", {"state": "idle"}))
        self.context.enter_context(patch.object(server, "RUNNER", Mock(running=False)))
        self.context.enter_context(patch.object(server, "ORG_MIGRATION", Mock(running=False)))
        self.context.enter_context(patch.object(server.config, "HOSTINGER_MAIL_PROFILES", self.profiles))
        self.context.enter_context(patch.object(server, "_list_accounts", return_value=[]))
        self.context.enter_context(patch.object(server.identity, "gerar_identidade", return_value=self.identity))
        self.thread = self.context.enter_context(patch.object(server.threading, "Thread"))
        self.client = server.create_app().test_client()

    def start(self, domain="example.invalid", count=1):
        return self.client.post("/api/accounts/bulk-register", json={"domain": domain, "count": count})

    def snapshot(self):
        return self.client.get("/api/accounts/bulk-register/status").get_json()

    def test_write_failure_terminates_batch_and_allows_retry(self):
        with patch.object(server.account_bans, "require_not_banned"), \
             patch.object(server.crowtado, "criar_conta"), \
             patch.object(server, "_set_account_removed"), \
             patch.object(server, "_save_crowtado_cred", side_effect=OSError("private diagnostic")):
            self.assertEqual(self.start().status_code, 200)
            self.thread.call_args.kwargs["target"]()
        state = self.snapshot()
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["completed"], 0)
        self.assertEqual(state["current_email"], "")
        self.assertEqual(state["current_step"], "")
        self.assertNotIn("private diagnostic", str(state))
        self.assertEqual(self.start().status_code, 200)
        self.assertNotIn("error", self.snapshot())

    def test_unexpected_failure_preserves_completed_results(self):
        with patch.object(server, "_full_register_account", side_effect=[
                {"steps": {}, "error": None}, ValueError("unexpected response")]):
            self.assertEqual(self.start(count=2).status_code, 200)
            self.thread.call_args.kwargs["target"]()
        state = self.snapshot()
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["completed"], 1)
        self.assertEqual(state["created"], 1)
        self.assertEqual(len(state["results"]), 1)

    def test_thread_start_failure_does_not_leave_running_state(self):
        self.thread.return_value.start.side_effect = RuntimeError("no thread available")
        self.assertEqual(self.start().status_code, 500)
        self.assertEqual(self.snapshot()["state"], "failed")
        self.thread.return_value.start.side_effect = None
        self.assertEqual(self.start().status_code, 200)

    def test_successful_batch_finishes_and_rejects_overlap(self):
        with patch.object(server, "_full_register_account", return_value={"steps": {}, "error": None}):
            self.assertEqual(self.start().status_code, 200)
            self.assertEqual(self.start().status_code, 409)
            self.thread.call_args.kwargs["target"]()
        self.assertEqual(self.snapshot()["state"], "done")
        self.assertEqual(self.snapshot()["created"], 1)

    def test_domain_list_excludes_email_routes_and_profiles_without_tokens(self):
        self.profiles.extend([
            {"id": "duplicate", "token": "test-only", "routes": ["example.invalid"]},
            {"id": "disabled", "token": "", "routes": ["disabled.invalid"]},
        ])
        domains = self.client.get("/api/accounts/domains").get_json()["domains"]
        self.assertEqual([item["domain"] for item in domains], ["example.invalid"])

    def test_email_route_and_unconfigured_domain_are_rejected_before_work(self):
        for domain in ("person@other.invalid", "other.invalid", "unknown.invalid"):
            with self.subTest(domain=domain):
                self.assertEqual(self.start(domain).status_code, 400)
        self.thread.assert_not_called()
        self.assertEqual(self.snapshot()["state"], "idle")
