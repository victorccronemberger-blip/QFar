"""Real upload_to_account replacement decisions with inert local fixtures."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from moneymin import campaign, config, upload
from moneymin.campaign_types import AccountSpec


class CampaignTerminalReplacementTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.journals = self.root / "sidecars"
        self.journals.mkdir()
        self.stack.enter_context(patch.object(config, "DATA_DIR", self.root))
        self.stack.enter_context(patch.object(upload, "sidecars_dir", return_value=self.journals))
        self.account = AccountSpec("fixture@example.invalid", "fixture-org")
        self.item = {
            "clip_uid": "fixture-clip", "registry_key": "minute|fixture-task",
            "duration_ms": 300_000, "video_path": str(self.root / "fixture.mp4"),
        }
        Path(self.item["video_path"]).write_bytes(b"inert local video fixture")
        self.session = Mock(email=self.account.email, _live=True, recording_policy=None)
        self.session._moneymin_pending_pumped = False
        self.profile = Mock()
        self.recorded_at = "2026-10-08T04:10:30.988Z"
        self._patch_upload_preparation()

    def _patch_upload_preparation(self):
        self.stack.enter_context(patch.object(campaign.org_policy, "account_kind", return_value="other"))
        self.stack.enter_context(patch.object(campaign.device_profile, "get_profile", return_value=self.profile))
        self.stack.enter_context(patch.object(campaign, "probe_video", return_value={
            "duration_ms": 300_000, "fps": 30.0}))
        self.stack.enter_context(patch.object(campaign, "build_imu_csv", return_value="timestamp,x,y,z\n"))
        self.stack.enter_context(patch.object(campaign, "build_frames_csv_from_video", return_value="frame\n"))
        self.stack.enter_context(patch.object(campaign, "_build_sidecar", return_value=b"inert sidecar"))
        self.stack.enter_context(patch.object(campaign, "_new_identity", return_value=(
            "replacement-session", "replacement-session_0", "2026-10-08T04:11:00.000Z")))

    def terminal_row(self, *, sid="terminal-session", context=None):
        row = {
            "session_id": sid, "chunk_index": 0, "expected_chunk_count": 1,
            "account_email": self.account.email, "org_key": self.account.org_key,
            "task_id": "fixture-task", "upload_id": f"upload-{sid}",
            "log_id": f"{sid}_0", "filename": f"{sid}_0.mp4",
            "recorded_at": self.recorded_at, "duration_ms": 300_000,
            "state": "failed", "phase": "remote_terminal_failure", "finalized": False,
            "register_first": True, "native_response_schema": True, "create_attempted": True,
            "campaign_reconciled": False,
            "campaign_context": context or {
                "registry_key": self.item["registry_key"],
                "clip_uid": self.item["clip_uid"], "task_id": "fixture-task"},
        }
        row["remote_terminal_failure"] = {
            "version": 1, "status": "failed", "upload_id": row["upload_id"],
            "session_id": sid, "log_id": row["log_id"], "email": self.account.email,
            "user_resource_key": "fixture-user-resource", "org_key": self.account.org_key,
            "task_id": "fixture-task", "recorded_at": self.recorded_at,
            "duration_ms": 300_000, "chunk_index": 0, "expected_chunk_count": 1,
            "checked_at": "2026-10-08T04:12:00.000Z",
        }
        return row

    def persist(self, *rows):
        for row in rows:
            upload.save_sidecar(row)

    def upload_new_session(self, *_args, **kwargs):
        chunk = SimpleNamespace(state="done", evaluate_result=None, upload_id="new-upload",
                                error=None)
        return SimpleNamespace(session_id="replacement-session", finalized=True,
                               finalize_status="completed", chunks=[chunk])

    def test_recover_pending_false_still_links_authoritative_terminal_parent(self):
        old = self.terminal_row()
        self.persist(old)
        upload_session = self.stack.enter_context(patch.object(
            campaign, "upload_session", side_effect=self.upload_new_session))
        pump = self.stack.enter_context(patch.object(
            campaign, "_pump_account_pending",
            side_effect=AssertionError("reviewed mode must omit the global pump")))
        with patch.object(campaign.sent_registry, "recovery_reset_checker",
                          return_value=lambda *_args: False):
            result = campaign.upload_to_account(
                self.item, self.account, "fixture-task", 30, True, True,
                session=self.session, recover_pending=False)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["session_id"], "replacement-session")
        self.assertEqual(upload_session.call_count, 1)
        context = upload_session.call_args.kwargs["campaign_context"]
        self.assertEqual(context["retry_of_session_id"], "terminal-session")
        pump.assert_not_called()
        self.assertTrue(upload.is_terminal_remote_failure(upload.load_sidecar("terminal-session")))

    def test_newer_pending_session_blocks_replacement_without_sending(self):
        old = self.terminal_row()
        newer = {
            **old, "session_id": "newer-pending-session", "upload_id": "newer-upload",
            "log_id": "newer-pending-session_0", "filename": "newer-pending-session_0.mp4",
            "state": "transport", "phase": "transport", "finalized": False,
            "remote_terminal_failure": None,
            "campaign_context": {**old["campaign_context"],
                                 "retry_of_session_id": "terminal-session"},
        }
        self.persist(old, newer)
        new_identity = self.stack.enter_context(patch.object(
            campaign, "_new_identity", side_effect=AssertionError("pending SID blocks creation")))
        upload_session = self.stack.enter_context(patch.object(
            campaign, "upload_session", side_effect=AssertionError("pending SID blocks upload")))
        with patch.object(campaign.sent_registry, "recovery_reset_checker",
                          return_value=lambda *_args: False):
            result = campaign.upload_to_account(
                self.item, self.account, "fixture-task", 30, True, True,
                session=self.session, recover_pending=False)

        self.assertFalse(result["ok"])
        self.assertEqual(result["session_id"], "newer-pending-session")
        self.assertIn("nova sessão não criada", result["error"])
        new_identity.assert_not_called()
        upload_session.assert_not_called()

    def test_forged_terminal_marker_does_not_authorize_replacement(self):
        forged = self.terminal_row()
        forged["remote_terminal_failure"]["upload_id"] = "different-upload"
        self.persist(forged)
        new_identity = self.stack.enter_context(patch.object(
            campaign, "_new_identity", side_effect=AssertionError("forged proof blocks creation")))
        upload_session = self.stack.enter_context(patch.object(
            campaign, "upload_session", side_effect=AssertionError("forged proof blocks upload")))
        with patch.object(campaign.sent_registry, "recovery_reset_checker",
                          return_value=lambda *_args: False):
            result = campaign.upload_to_account(
                self.item, self.account, "fixture-task", 30, True, True,
                session=self.session, recover_pending=False)

        self.assertFalse(result["ok"])
        self.assertEqual(result["session_id"], "terminal-session")
        new_identity.assert_not_called()
        upload_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
