"""Unreadable or mismatched disk journals block automatic transport safely."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from moneymin import campaign, config, recovery, upload


class UploadJournalIntegrityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.journals = self.root / "sidecars"
        self.journals.mkdir()
        for mocked in (patch.object(config, "DATA_DIR", self.root),
                       patch.object(config, "MEDIA_DATA_DIR", self.root),
                       patch.object(upload, "sidecars_dir", return_value=self.journals),
                       patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))):
            mocked.start()
            self.addCleanup(mocked.stop)
        self.row = {"session_id": "session1", "chunk_index": 0,
                    "account_email": "one@example.com", "org_key": "org",
                    "task_id": "task", "state": upload.STATE_COMPLETING,
                    "phase": "awaiting_finalize", "upload_id": "upload",
                    "finalize_requested": True, "expected_chunk_count": 1,
                    "campaign_context": {"registry_key": "task", "clip_uid": "clip"}}
        self.valid = self.journals / "session1.json"
        self.valid.write_text(json.dumps(self.row), encoding="utf-8")

    def disk_bytes(self):
        return {path.name: path.read_bytes() for path in self.journals.iterdir()}

    def test_listing_excludes_concurrent_checkpoint_replacement(self):
        from moneymin.media_lifecycle import media_state_lease
        from moneymin.operation_lease import OperationLeaseError
        entered, release = threading.Event(), threading.Event()
        result, errors = [], []
        original = upload._read_sidecar_file

        def read(path):
            entered.set()
            if not release.wait(3):
                raise AssertionError('Listing fixture was not released')
            return original(path)

        def listing():
            try:
                result.extend(upload.list_sidecars())
            except Exception as exc:
                errors.append(exc)

        def write():
            try:
                upload.save_sidecar({**self.row, 'state': 'done'})
            except Exception as exc:
                errors.append(exc)

        writer = threading.Thread(target=write)
        with patch.object(upload, '_read_sidecar_file', side_effect=read):
            reader = threading.Thread(target=listing)
            reader.start()
            try:
                self.assertTrue(entered.wait(3))
                # The same barrier used by save_sidecar is already owned by
                # the listing thread, before a checkpoint can be replaced.
                with self.assertRaises(OperationLeaseError):
                    with media_state_lease():
                        pass
                writer.start()
            finally:
                release.set()
                reader.join(3)
                if writer.ident is not None:
                    writer.join(3)
        self.assertFalse(reader.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result, [self.row])
        self.assertEqual(upload.list_sidecars()[0]['state'], 'done')

    def assert_loader_and_pump_blocked(self):
        before = self.disk_bytes()
        messages = []
        for state in (None, "creating", "done"):
            with self.subTest(state=state), self.assertRaises(upload.UploadError) as raised:
                upload.list_sidecars(state)
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(raised.exception.phase, "recovery")
            messages.append(str(raised.exception))
        with patch.object(upload, "save_sidecar") as save, \
             patch.object(upload, "upload_session") as send, \
             patch.object(upload, "complete_upload") as complete, \
             patch.object(upload, "evaluate_upload") as evaluate, \
             patch.object(upload, "_finalize_session") as finalize, \
             patch.object(upload, "_remove_sidecar_archive") as remove:
            with self.assertRaises(upload.UploadError) as raised:
                upload.pump_pending(SimpleNamespace(email="one@example.com"), required_org_key="org")
            messages.append(str(raised.exception))
            for service in (save, send, complete, evaluate, finalize, remove):
                service.assert_not_called()
        self.assertEqual(len(set(messages)), 1)
        for message in messages:
            self.assertNotIn("private-token", message)
            self.assertNotIn(str(self.root), message)
            self.assertNotIn("broken.json", message)
        self.assertEqual(self.disk_bytes(), before)

    def test_invalid_utf8_and_malformed_json_are_not_silently_omitted(self):
        broken = self.journals / "broken.json"
        for payload in (b"\xff\xfe-private-token", b'{"private-token":'):
            with self.subTest(payload=payload):
                broken.write_bytes(payload)
                self.assert_loader_and_pump_blocked()

    def test_nonobject_json_root_requires_review(self):
        broken = self.journals / "broken.json"
        for payload in ([], None, 1, "private-token"):
            with self.subTest(payload=payload):
                broken.write_text(json.dumps(payload), encoding="utf-8")
                self.assert_loader_and_pump_blocked()

    def test_filename_mismatch_or_invalid_identity_blocks_even_other_owner(self):
        broken = self.journals / "broken.json"
        for changes in ({"session_id": "another"}, {"session_id": []},
                        {"session_id": "../private-token"}, {"chunk_index": True},
                        {"chunk_index": "0"}, {"chunk_index": -1}):
            row = {**self.row, "account_email": "other@example.com", "state": "done", **changes}
            with self.subTest(changes=changes):
                broken.write_text(json.dumps(row), encoding="utf-8")
                self.assert_loader_and_pump_blocked()

    def test_read_failure_is_private_and_preserves_every_journal(self):
        original = Path.read_text

        def unreadable(path, *args, **kwargs):
            if path == self.valid:
                raise PermissionError("private-token in OS diagnostic")
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_text", unreadable):
            self.assert_loader_and_pump_blocked()

    def test_valid_legacy_index_zero_bom_and_foreign_owner_remain_listable(self):
        legacy = {"session_id": "legacy", "state": "done", "account_email": "other@example.com"}
        (self.journals / "legacy.json").write_text(json.dumps(legacy), encoding="utf-8-sig")
        self.assertEqual(upload.list_sidecars("done"), [legacy])
        self.assertEqual(len(upload.list_sidecars()), 2)

    def test_directory_read_failure_does_not_become_empty_list(self):
        before = self.disk_bytes()
        with patch.object(Path, "iterdir", side_effect=PermissionError("private-token")), \
             patch.object(upload, "save_sidecar") as save, \
             patch.object(upload, "upload_session") as send:
            for operation in (upload.list_sidecars,
                              lambda: upload.pump_pending(SimpleNamespace(email="one@example.com"))):
                with self.assertRaises(upload.UploadError) as raised:
                    operation()
                self.assertEqual(raised.exception.phase, "recovery")
                self.assertFalse(raised.exception.retryable)
                self.assertNotIn("private-token", str(raised.exception))
            save.assert_not_called()
            send.assert_not_called()
        self.assertEqual(self.disk_bytes(), before)

    def test_campaign_cannot_create_new_session_when_existing_json_is_unreadable(self):
        (self.journals / "broken.json").write_text('{"private-token":', encoding="utf-8")
        before = self.disk_bytes()
        account = SimpleNamespace(email="one@example.com", org_key="org")
        session = Mock(email=account.email, _live=True, _moneymin_pending_pumped=False)
        with patch.object(campaign.org_policy, "account_kind", return_value="claru"), \
             patch.object(campaign.device_profile, "get_profile", return_value=Mock()), \
             patch.object(campaign, "_new_identity") as identity, \
             patch.object(campaign, "upload_session") as send, \
             patch.object(upload, "save_sidecar") as save, \
             patch.object(upload, "complete_upload") as complete, \
             patch.object(upload, "_finalize_session") as finalize:
            result = campaign.upload_to_account({"clip_uid": "clip"}, account, "task", 1,
                                                evaluate=False, finalize=True, session=session)
        self.assertFalse(result["ok"])
        self.assertNotIn("private-token", result["error"])
        for service in (identity, send, save, complete, finalize):
            service.assert_not_called()
        self.assertEqual(self.disk_bytes(), before)

    def test_recovery_ui_keeps_independent_corruption_and_quality_diagnostics(self):
        from moneymin.web import server
        client = server.create_app(for_testing=True).test_client()
        broken = self.journals / "broken.json"
        broken.write_bytes(b"\xff-private-token")
        with self.assertRaises(ValueError):
            recovery.snapshot()
        response = client.get("/api/recovery")
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("private-token", json.dumps(response.get_json()))
        broken.unlink()
        row = {**self.row, "state": "done", "phase": "done", "finalized": True,
               "evaluation_required": True, "evaluation_verified": False}
        self.valid.write_text(json.dumps(row), encoding="utf-8")
        self.assertEqual(upload.list_sidecars(), [row])
        response = client.get("/api/recovery")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["items"][0]["status"], "needs_review")
        self.assertFalse(response.get_json()["items"][0]["can_resume"])


if __name__ == "__main__":
    unittest.main()
