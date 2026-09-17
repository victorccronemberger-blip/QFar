import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from moneymin import config
from moneymin.web import server
from moneymin.web.org_migration import OrgMigrationRunner


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.report = Path(self.tmp.name) / "migration.json"
        self.runner = OrgMigrationRunner(self.report)

    def wait(self):
        self.runner._thread.join(timeout=5)
        self.assertFalse(self.runner._thread.is_alive())

    def test_claru_skipped_without_login_join_or_preference_change(self):
        for email in ("USER@Supply.Claru.AI", "user@claru.ai", "user@other.claru.ai"):
            with self.subTest(email=email), \
                 mock.patch.object(server.Session, "from_email") as session, \
                 mock.patch.object(server, "_save_prefs") as save:
                row = server._migrate_account_org(email)
            self.assertEqual(row["status"], "skipped")
            self.assertEqual(row["account_kind"], "claru")
            session.assert_not_called()
            save.assert_not_called()

    def test_crowtado_updates_only_after_profile_confirms_target(self):
        for already in (False, True):
            with self.subTest(already=already):
                sess = mock.Mock()
                sess.ensure_auth.return_value = {"organizations": [
                    {"resourceKey": config.ORG_KEY if already else config.HUB_ORG_KEY}]}
                sess.join_org.return_value = (200, "{}")
                sess.me.return_value = {"organizations": [{"resourceKey": config.ORG_KEY}]}
                with mock.patch.object(server.Session, "from_email", return_value=sess), \
                     mock.patch.object(server, "_load_prefs", return_value={"other": True}), \
                     mock.patch.object(server, "_save_prefs") as save:
                    row = server._migrate_account_org("crow@example.com")
                self.assertEqual(row["status"], "already" if already else "migrated")
                save.assert_called_once_with({"other": True, "org_keys": {"crow@example.com": config.ORG_KEY}})
                if already:
                    sess.join_org.assert_not_called()
                else:
                    sess.join_org.assert_called_once_with(config.INVITE_CODE)

    def test_successful_join_without_membership_is_an_error(self):
        sess = mock.Mock()
        sess.ensure_auth.return_value = {"organizations": [{"resourceKey": config.HUB_ORG_KEY}]}
        sess.join_org.return_value = (200, config.ORG_KEY)
        sess.me.return_value = sess.ensure_auth.return_value
        with mock.patch.object(server.Session, "from_email", return_value=sess), \
             mock.patch.object(server, "_save_prefs") as save:
            with self.assertRaises(RuntimeError):
                server._migrate_account_org("crow@example.com")
        save.assert_not_called()

    def test_batch_isolates_errors_and_saves_safe_report(self):
        def migrate(email):
            if email == "bad@example.com":
                raise RuntimeError("timeout password=secret-password token=private-token")
            return {"email": email, "account_kind": "crowtado", "status": "migrated", "message": "OK"}
        self.runner.start(["bad@example.com", "good@example.com"], migrate)
        self.wait()
        state = self.runner.snapshot()
        self.assertEqual(state["state"], "completed")
        self.assertEqual(state["completed"], 2)
        self.assertEqual(state["counts"], {"error": 1, "migrated": 1})
        content = self.report.read_text(encoding="utf-8")
        self.assertNotIn("secret-password", content)
        self.assertNotIn("private-token", content)
        self.assertEqual(OrgMigrationRunner(self.report).snapshot(), state)
        state["results"].clear()
        self.assertEqual(len(self.runner.snapshot()["results"]), 2)

    def test_duplicate_start_does_not_replace_active_batch(self):
        release = threading.Event()
        self.addCleanup(release.set)
        def migrate(email):
            release.wait(3)
            return {"email": email, "status": "already"}
        self.runner.start(["a@example.com"], migrate)
        with self.assertRaises(RuntimeError):
            self.runner.start(["b@example.com"], migrate)
        release.set()
        self.wait()
        self.assertEqual(self.runner.snapshot()["results"][0]["email"], "a@example.com")

    def test_interrupted_report_can_be_viewed_after_restart(self):
        self.report.write_text(json.dumps({"state": "running", "completed": 1, "total": 2, "results": []}))
        restarted = OrgMigrationRunner(self.report)
        self.assertFalse(restarted.running)
        self.assertEqual(restarted.snapshot()["state"], "interrupted")
        self.assertEqual(restarted.snapshot()["completed"], 1)

    def test_no_remote_change_when_initial_report_cannot_be_saved(self):
        migrate = mock.Mock()
        with mock.patch("moneymin.web.org_migration.save_json", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.runner.start(["a@example.com"], migrate)
        self.assertFalse(self.runner.running)
        migrate.assert_not_called()

    def test_endpoint_starts_mixed_batch_and_preserves_claru(self):
        emails = ["crow@example.com", "user@supply.claru.ai"]
        sess = mock.Mock()
        sess.ensure_auth.return_value = {"organizations": [{"resourceKey": config.ORG_KEY}]}
        with mock.patch.object(server, "ORG_MIGRATION", self.runner), \
             mock.patch.object(server, "RUNNER", mock.Mock(running=False)), \
             mock.patch.object(server, "BALANCES_RUNNER", mock.Mock(running=False)), \
             mock.patch.object(server, "_list_accounts", return_value=[{"email": email} for email in emails]), \
             mock.patch.object(server.Session, "from_email", return_value=sess) as auth, \
             mock.patch.object(server, "_load_prefs", return_value={}), \
             mock.patch.object(server, "_save_prefs"):
            client = server.create_app().test_client()
            response = client.post("/api/accounts/migration", json={})
            self.assertEqual(response.status_code, 202)
            self.wait()
            result = client.get("/api/accounts/migration").get_json()
        self.assertEqual(result["counts"], {"already": 1, "skipped": 1})
        auth.assert_called_once_with("crow@example.com")

    def test_unknown_accounts_and_busy_campaign_do_not_start_migration(self):
        with mock.patch.object(server, "ORG_MIGRATION", self.runner), \
             mock.patch.object(server, "RUNNER", mock.Mock(running=False)) as campaign, \
             mock.patch.object(server, "BALANCES_RUNNER", mock.Mock(running=False)), \
             mock.patch.object(server, "_list_accounts", return_value=[{"email": "a@example.com"}]), \
             mock.patch.object(server, "_migrate_account_org") as migrate:
            client = server.create_app().test_client()
            self.assertEqual(client.post("/api/accounts/migration", json={"emails": ["unknown@example.com"]}).status_code, 400)
            campaign.running = True
            self.assertEqual(client.post("/api/accounts/migration", json={}).status_code, 409)
        migrate.assert_not_called()

    def test_account_and_campaign_changes_blocked_during_migration(self):
        with mock.patch.object(server, "ORG_MIGRATION", mock.Mock(running=True)):
            client = server.create_app().test_client()
            for route in ("/api/accounts/migration", "/api/accounts/check-all", "/api/accounts/import", "/api/campaigns"):
                with self.subTest(route=route):
                    self.assertEqual(client.post(route, json={}).status_code, 409)
            self.assertEqual(client.delete("/api/accounts/a@example.com").status_code, 409)

    def test_account_list_classifies_by_email_not_cached_org(self):
        root = Path(self.tmp.name)
        for index, email in enumerate(("crow@example.com", "user@supply.claru.ai")):
            (root / f"token_{index}.json").write_text(json.dumps({"email": email}))
        with mock.patch.object(server.config, "tokens_dir", return_value=root), \
             mock.patch.object(server, "_removed_accounts", return_value=set()), \
             mock.patch.object(server, "_load_prefs", return_value={"org_keys": {"user@supply.claru.ai": config.ORG_KEY}}):
            rows = server._list_accounts()
        self.assertEqual([row["account_kind"] for row in rows], ["crowtado", "claru"])
