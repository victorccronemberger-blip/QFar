import unittest
from unittest import mock

from moneymin import campaign, config, upload
from moneymin.campaign_types import AccountSpec
from moneymin.minute_api import AuthError
from moneymin.web import server


class CampaignOrgGuardTests(unittest.TestCase):
    def test_cached_new_org_does_not_bypass_live_membership(self):
        sess = mock.Mock()
        sess.ensure_auth.return_value = {
            "organizations": [{"resourceKey": config.HUB_ORG_KEY}],
        }
        sess.join_org.return_value = (200, "{}")
        sess.me.return_value = sess.ensure_auth.return_value
        with mock.patch.object(server, "_load_prefs", return_value={
                "org_keys": {"crow@example.com": config.ORG_KEY}}), \
             mock.patch.object(server, "_save_prefs") as save:
            with self.assertRaises(RuntimeError):
                server._resolve_org("crow@example.com", session=sess)
        save.assert_not_called()

    def test_campaign_does_not_start_when_one_account_fails_org_validation(self):
        runner = mock.Mock(running=False)
        def resolve(email):
            if email == "bad@example.com":
                raise RuntimeError("organização nova ausente")
            return config.ORG_KEY
        with mock.patch.object(server, "RUNNER", runner), \
             mock.patch.object(server, "HOLO_CACHE_RUNNER", mock.Mock(running=False)), \
             mock.patch.object(server, "_list_accounts", return_value=[
                 {"email": "good@example.com"}, {"email": "bad@example.com"}]), \
             mock.patch.object(server, "_resolve_org", side_effect=resolve), \
             mock.patch.object(server.readiness, "campaign_readiness", return_value={"checks": []}), \
             mock.patch.object(server.campaign, "available_tasks") as tasks:
            response = server.create_app().test_client().post("/api/campaigns", json={
                "accounts": ["good@example.com", "bad@example.com"],
                "tasks": [{"task_id": "task"}], "dataset": "ego4d",
            })
        self.assertEqual(response.status_code, 400)
        self.assertIn("campanha bloqueada", response.get_json()["error"])
        runner.start.assert_not_called()
        tasks.assert_not_called()

    def test_stale_crowtado_destination_is_blocked_before_any_upload(self):
        with mock.patch.object(campaign.Session, "from_email") as auth, \
             mock.patch.object(campaign, "pump_pending") as pending, \
             mock.patch.object(campaign, "upload_session") as send:
            result = campaign.upload_to_account(
                {}, AccountSpec("crow@example.com", config.HUB_ORG_KEY),
                task_id="task", timeout_blob=30, evaluate=False, finalize=True,
            )
        self.assertFalse(result["ok"])
        self.assertIn(config.INVITE_CODE, result["error"])
        auth.assert_not_called()
        pending.assert_not_called()
        send.assert_not_called()

    def test_pending_recovery_uses_selected_org_and_exempts_claru_from_crowtado_gate(self):
        for email, key in (("crow@example.com", config.ORG_KEY),
                           ("user@supply.claru.ai", config.CLARU_ORG_KEY)):
            with self.subTest(email=email):
                sess = mock.Mock(_live=True, _moneymin_pending_pumped=False)
                with mock.patch.object(campaign.device_profile, "get_profile"), \
                     mock.patch.object(campaign, "pump_pending", return_value=[]) as pending, \
                     mock.patch.object(campaign, "_new_identity", side_effect=AuthError("test stop")):
                    result = campaign.upload_to_account(
                        {"duration_ms": 60000}, AccountSpec(email, key),
                        task_id="task", timeout_blob=30, evaluate=False, finalize=True,
                        session=sess,
                    )
                pending.assert_called_once_with(sess, account_email=email, required_org_key=key)
                sess.join_org.assert_not_called()
                self.assertEqual(result["error"], "test stop")

    def test_old_pending_upload_is_preserved_without_retry_or_finalize(self):
        pending = {
            "account_email": "crow@example.com", "org_key": config.HUB_ORG_KEY,
            "state": upload.STATE_COMPLETING, "phase": "awaiting_finalize",
            "session_id": "old-session", "chunk_index": 0, "finalize_requested": True,
        }
        with mock.patch.object(upload, "list_sidecars", return_value=[pending]), \
             mock.patch.object(upload, "save_sidecar") as save, \
             mock.patch.object(upload, "upload_session") as send, \
             mock.patch.object(upload, "_finalize_session") as finalize:
            result = upload.pump_pending(
                mock.Mock(), account_email="crow@example.com", required_org_key=config.ORG_KEY,
            )
        self.assertEqual(result, [])
        save.assert_not_called()
        send.assert_not_called()
        finalize.assert_not_called()
