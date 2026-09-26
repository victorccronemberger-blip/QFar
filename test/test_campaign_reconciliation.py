import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from contextlib import ExitStack

from moneymin import campaign, config, sent_registry
from moneymin import upload
from moneymin.campaign_types import AccountSpec


class CampaignReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(config, "DATA_DIR", root))
        self.account = AccountSpec("account@example.com", config.ORG_KEY)
        self.item = {"clip_uid": "clip", "registry_key": "key", "duration_ms": 300000}
        self.rows = [{"session_id": "old-session", "chunk_index": 0, "expected_chunk_count": 1,
                      "account_email": self.account.email, "org_key": self.account.org_key,
                      "task_id": "task", "state": "done", "phase": "done", "finalized": True,
                      "campaign_context": {"registry_key": "key", "clip_uid": "clip"}}]
        self.stack.enter_context(patch.object(campaign, "list_sidecars", side_effect=lambda: self.rows))
        self.save = self.stack.enter_context(patch.object(campaign, "save_sidecar"))
        self.stack.enter_context(patch.object(campaign.device_profile, "get_profile", return_value=Mock()))
        self.stack.enter_context(patch.object(campaign, "pump_pending", side_effect=lambda *a, **k: self.rows))
        self.new = self.stack.enter_context(patch.object(campaign, "_new_identity", side_effect=AssertionError("duplicate session")))
        self.upload = self.stack.enter_context(patch.object(campaign, "upload_session", side_effect=AssertionError("network forbidden")))

    def run_upload(self):
        session = Mock(_live=True, recording_policy=None)
        session._moneymin_pending_pumped = False
        return campaign.upload_to_account(self.item, self.account, "task", 30, True, True, session=session)

    def test_recovered_session_replaces_new_upload_and_updates_registry(self):
        result = self.run_upload()
        self.assertTrue(result["ok"])
        self.assertTrue(result["recovered"])
        self.assertEqual(result["session_id"], "old-session")
        self.assertEqual(sent_registry.sent_emails("key", "clip"), {self.account.email})
        self.assertTrue(self.rows[0]["campaign_reconciled"])
        self.new.assert_not_called()
        self.upload.assert_not_called()

    def test_incomplete_or_unfinalized_session_blocks_new_upload(self):
        for updates in ({"finalized": False}, {"expected_chunk_count": 2}, {"state": "retry-late"}):
            with self.subTest(updates=updates):
                original = dict(self.rows[0])
                self.rows[0].update(updates)
                result = self.run_upload()
                self.assertFalse(result["ok"])
                self.assertEqual(result["session_id"], "old-session")
                self.assertFalse(sent_registry.sent_emails("key", "clip"))
                self.new.assert_not_called()
                self.rows[0] = original

    def test_all_chunks_must_be_finalized(self):
        self.rows[0]["expected_chunk_count"] = 2
        self.rows.append({**self.rows[0], "chunk_index": 1})
        result = self.run_upload()
        self.assertTrue(result["ok"])
        self.assertEqual(self.save.call_count, 2)

    def test_crash_after_finalize_before_registry_is_reconciled(self):
        with patch.object(campaign, "pump_pending", return_value=[]):
            self.assertTrue(self.run_upload()["ok"])
        self.new.assert_not_called()

    def test_reset_prevents_old_completed_journal_from_repopulating_registry(self):
        with patch.object(upload, "list_sidecars", return_value=self.rows):
            sent_registry.reset()
        result = campaign._reconcile_uploads(self.rows, self.account, self.item, "task")
        self.assertIsNone(result)
        self.assertFalse(sent_registry.sent_emails("key", "clip"))

    def test_reset_does_not_duplicate_an_upload_still_pending_at_reset(self):
        self.rows[0]["finalized"] = False
        self.rows[0]["state"] = "completing"
        with patch.object(upload, "list_sidecars", return_value=self.rows):
            sent_registry.reset()
        result = self.run_upload()
        self.assertFalse(result["ok"])
        self.new.assert_not_called()

    def test_foreign_account_is_never_reconciled(self):
        self.rows[0]["account_email"] = "another@example.com"
        self.assertIsNone(campaign._reconcile_uploads(self.rows, self.account, self.item, "task"))
        self.assertFalse(sent_registry.load())
        self.save.assert_not_called()

    def test_reset_keeps_missing_chunks_pending(self):
        self.rows[0]["expected_chunk_count"] = 2
        with patch.object(upload, "list_sidecars", return_value=self.rows):
            sent_registry.reset()
        self.assertFalse(self.run_upload()["ok"])
        self.new.assert_not_called()

    def test_category_reset_preserves_other_category_recovery(self):
        with patch.object(upload, "list_sidecars", return_value=self.rows):
            sent_registry.reset("other-category")
        self.assertTrue(self.run_upload()["ok"])
