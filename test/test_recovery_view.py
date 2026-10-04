import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from moneymin import config, recovery, sent_registry, upload


class RecoveryViewTests(unittest.TestCase):
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
        self.row = {"account_email": "one@example.com", "org_key": "org", "session_id": "session1",
                    "chunk_index": 0, "expected_chunk_count": 1, "task_id": "task",
                    "upload_id": "accepted-session1-upload", "state": "done", "finalized": True,
                    "campaign_context": {"registry_key": "task", "clip_uid": "clip", "task_id": "task"}}

    def save(self, row=None, name="session1.json"):
        (self.journals / name).write_text(json.dumps(row or self.row), encoding="utf-8")

    def assert_history_pending(self, result, count=1):
        # An index ACK without its historical publication remains visible.
        self.assertEqual(len(result["items"]), count)
        self.assertEqual(result["publication_pending"], count)
        self.assertEqual(result["reconciliation_pending"], 0)
        self.assertTrue(all(row["status"] == "confirmed" and row["index_reconciled"] is True
                            and row["publication_pending"] is True and row["can_resume"] is False
                            for row in result["items"]))

    def test_large_legacy_batch_reads_history_once_and_resolves_directory_once(self):
        accounts = []
        for index in range(100):
            sid = f"session{index}"
            row = {**self.row, "session_id": sid}
            row.pop("campaign_context")
            self.save(row, name=f"{sid}.json")
            accounts.append({"session_id": sid, "email": self.row["account_email"]})
        history = self.root / "campaign_old.json"
        history.write_text(json.dumps({"items": [{"clip_uid": "clip", "registry_key": "task",
            "task_id": "task", "accounts": accounts}]}), encoding="utf-8")
        original = Path.read_text
        reads = []
        def read(path, *args, **kwargs):
            reads.append(path)
            return original(path, *args, **kwargs)
        with patch.object(Path, "read_text", read), patch.object(upload, "sidecars_dir", return_value=self.journals) as directory:
            result = recovery.snapshot()
        self.assertEqual(result["confirmed"], 100)
        self.assertEqual(reads.count(history), 1)
        directory.assert_called_once()

    def test_snapshot_does_not_export_secrets_or_paths(self):
        self.save({**self.row, "blob_url": "secret-signed-url", "video_path": "C:/private/file",
                   "error": "secret-token"})
        snapshot = recovery.snapshot()
        self.assertEqual(snapshot["confirmed"], 1)
        self.assertNotIn("secret", json.dumps(snapshot))
        self.assertNotIn("C:/private", json.dumps(snapshot))

    def test_listing_reads_resets_once_and_preserves_reset_exclusions(self):
        for index in range(10):
            self.save({**self.row, "session_id": f"session{index}"}, name=f"session{index}.json")
        reset_path = self.root / "sent_reset_history.json"
        reset_path.write_text(json.dumps({"completed_sessions": ["session0"]}), encoding="utf-8")
        with patch.object(sent_registry, "_reset_history", wraps=sent_registry._reset_history) as reads:
            result = recovery.snapshot()
        self.assertEqual(result["confirmed"], 9)
        reads.assert_called_once()
        self.assertNotIn("session0", [item["session_id"] for item in result["items"]])

    def test_legacy_media_mapping_requires_unique_clip_task_and_account(self):
        row = {**self.row, "campaign_context": None, "local_video_path": "C:/old/clip_native.mp4"}
        self.save(row)
        history = {"items": [{"clip_uid": "clip", "registry_key": "task", "task_id": "task",
                    "video_path": "clip_native.mp4", "accounts": [{"email": "one@example.com"}]}]}
        path = self.root / "campaign_legacy.json"
        path.write_text(json.dumps(history), encoding="utf-8")
        result = recovery.snapshot()
        self.assertEqual(result["confirmed"], 1)
        self.assertFalse(result["items"][0]["blocks_campaign"])
        history["items"].append({**history["items"][0], "clip_uid": "different"})
        path.write_text(json.dumps(history), encoding="utf-8")
        self.assertTrue(recovery.snapshot()["items"][0]["blocks_campaign"])
        history["items"] = [{**history["items"][0], "task_id": "different-task"}]
        path.write_text(json.dumps(history), encoding="utf-8")
        self.assertTrue(recovery.snapshot()["items"][0]["blocks_campaign"])

    def test_uncertain_known_clip_is_reserved_without_marking_sent(self):
        self.save({**self.row, "state": "failed", "finalized": False})
        result = recovery.snapshot()
        self.assertEqual(result["pending"], 1)
        self.assertFalse(result["items"][0]["blocks_campaign"])
        self.assertEqual(recovery.campaign_exclusions(result["items"]), {"clip": ["one@example.com"]})
        self.assertEqual(sent_registry.sent_emails("task", "clip"), set())
        self.assertEqual(recovery.reconcile_confirmed()["reconciled"], 0)

    def test_conflicting_contexts_keep_account_blocked(self):
        self.save({**self.row, "expected_chunk_count": 2})
        self.save({**self.row, "chunk_index": 1, "expected_chunk_count": 2,
                   "campaign_context": {"registry_key": "task", "clip_uid": "other"}}, "session1__1.json")
        self.assertTrue(recovery.snapshot()["items"][0]["blocks_campaign"])

    def test_batch_reconciliation_writes_sent_index_once_and_retries_failed_ack(self):
        for index in range(100):
            self.save({**self.row, "session_id": f"session{index}"}, name=f"session{index}.json")
        with patch.object(sent_registry, "_save", wraps=sent_registry._save) as saved, \
             patch.object(recovery, "save_json", side_effect=OSError("interrupted acknowledgment")):
            with self.assertRaises(OSError):
                recovery.reconcile_confirmed()
        saved.assert_called_once()
        self.assertEqual(sent_registry.sent_emails("task", "clip"), {"one@example.com"})
        with patch.object(sent_registry, "_save", wraps=sent_registry._save) as saved, \
             patch.object(upload, "sidecars_dir", return_value=self.journals) as directory:
            result = recovery.reconcile_confirmed()
        saved.assert_not_called()
        self.assertEqual(directory.call_count, 2)
        self.assertEqual(result["reconciled"], 100)
        self.assert_history_pending(result, 100)

    def test_confirmation_is_reconciled_once_without_network(self):
        self.save()
        result = recovery.reconcile_confirmed()
        self.assertEqual(result["reconciled"], 1)
        self.assert_history_pending(result)
        self.assertEqual(sent_registry.sent_emails("task", "clip"), {"one@example.com"})
        self.assertEqual(recovery.reconcile_confirmed()["reconciled"], 0)

    def test_incomplete_chunk_group_is_never_marked_sent(self):
        self.save({**self.row, "expected_chunk_count": 2})
        result = recovery.reconcile_confirmed()
        self.assertEqual(result["pending"], 1)
        self.assertEqual(result["reconciled"], 0)
        self.assertEqual(sent_registry.sent_emails("task", "clip"), set())

    def test_partially_acknowledged_group_recovers_remaining_ack(self):
        self.save({**self.row, "expected_chunk_count": 2, "campaign_reconciled": True})
        self.save({**self.row, "expected_chunk_count": 2, "chunk_index": 1}, "session1__1.json")
        self.assertEqual(recovery.reconcile_confirmed()["reconciled"], 1)
        self.assert_history_pending(recovery.snapshot())

    def test_corrupt_journal_does_not_disappear_from_recovery(self):
        (self.journals / "broken.json").write_text("{", encoding="utf-8")
        with self.assertRaises(ValueError):
            recovery.snapshot()
        with self.assertRaises(ValueError):
            recovery.reconcile_confirmed()

    def test_transport_completion_is_not_finalization(self):
        self.save({**self.row, "state": "transport_done", "finalized": False})
        result = recovery.reconcile_confirmed()
        self.assertEqual(result["pending"], 1)
        self.assertEqual(result["reconciled"], 0)
        self.assertEqual(sent_registry.sent_emails("task", "clip"), set())

    def test_conflicting_session_ownership_blocks_reconciliation(self):
        self.save({**self.row, "expected_chunk_count": 2})
        self.save({**self.row, "account_email": "other@example.com", "chunk_index": 1,
                   "expected_chunk_count": 2}, "session1__1.json")
        with self.assertRaises(ValueError):
            recovery.reconcile_confirmed()
        self.assertEqual(sent_registry.sent_emails("task", "clip"), set())

    def test_api_blocks_reconciliation_during_live_campaign(self):
        from moneymin.web import server
        self.save()
        with patch.object(server, "RUNNER", SimpleNamespace(running=True)):
            client = server.create_app(for_testing=True).test_client()
            self.assertEqual(client.get("/api/recovery").status_code, 200)
            self.assertEqual(client.post("/api/recovery/reconcile", json={}).status_code, 409)
        self.assertEqual(recovery.snapshot()["confirmed"], 1)

    def test_api_reconciles_and_hides_raw_read_errors(self):
        from moneymin.web import server
        self.save()
        with patch.object(server, "RUNNER", SimpleNamespace(running=False)):
            client = server.create_app(for_testing=True).test_client()
            response = client.post("/api/recovery/reconcile", json={})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["reconciled"], 1)
            with patch.object(recovery, "snapshot", side_effect=OSError("secret-path-token")):
                response = client.get("/api/recovery")
            self.assertEqual(response.status_code, 409)
            self.assertNotIn("secret-path-token", response.get_data(as_text=True))

    def test_required_quality_verification_cannot_be_reconciled_as_sent(self):
        for extra in ({}, {"evaluation_verified": False}, {"evaluation_verified": None}):
            with self.subTest(extra=extra):
                self.save({**self.row, "schema_version": 2, "evaluation_required": True, **extra})
                result = recovery.reconcile_confirmed()
                self.assertEqual(result["confirmed"], 0)
                self.assertEqual(result["reconciled"], 0)
                self.assertEqual(result["items"][0]["status"], "needs_review")
                self.assertFalse(result["items"][0]["can_resume"])
                self.assertEqual(recovery.campaign_exclusions(result["items"]),
                                 {"clip": ["one@example.com"]})
                self.assertEqual(sent_registry.sent_emails("task", "clip"), set())

    def test_verified_required_quality_receipt_is_reconciled(self):
        self.save({**self.row, "schema_version": 2,
                   "evaluation_required": True, "evaluation_verified": True})
        self.assertEqual(recovery.reconcile_confirmed()["reconciled"], 1)
        self.assertEqual(sent_registry.sent_emails("task", "clip"), {"one@example.com"})

    def test_acknowledgment_cannot_hide_inconsistent_quality_or_flag_types(self):
        for extra in ({"evaluation_required": True, "evaluation_verified": False},
                      {"evaluation_required": "false"}):
            with self.subTest(extra=extra):
                self.save({**self.row, "campaign_reconciled": True, **extra})
                result = recovery.reconcile_confirmed()
                self.assertEqual(result["reconciled"], 0)
                self.assertEqual(result["items"][0]["status"], "needs_review")
                self.assertEqual(sent_registry.sent_emails("task", "clip"), set())

    def test_optional_quality_and_legacy_receipts_keep_finalization_semantics(self):
        for extra in ({}, {"evaluation_required": False, "evaluation_verified": False}):
            with self.subTest(extra=extra):
                self.save({**self.row, **extra})
                self.assertEqual(recovery.snapshot()["confirmed"], 1)

    def test_negative_unknown_quality_or_review_phase_is_not_confirmation(self):
        for extra in ({"evaluation_verified": False},
                      {"phase": "evaluation_review", "evaluation_required": False}):
            with self.subTest(extra=extra):
                self.save({**self.row, **extra})
                self.assertEqual(recovery.reconcile_confirmed()["reconciled"], 0)
                self.assertEqual(sent_registry.sent_emails("task", "clip"), set())

    def test_invalid_boolean_flags_require_review_without_reconciliation(self):
        for flag in ("finalized", "finalize_requested", "evaluation_required",
                     "evaluation_verified", "campaign_reconciled", "register_first",
                     "suppress_per_chunk_catbear"):
            for value in ("true", 1, 0, None, [], {}):
                with self.subTest(flag=flag, value=value):
                    self.save({**self.row, flag: value})
                    result = recovery.reconcile_confirmed()
                    self.assertEqual(result["reconciled"], 0)
                    self.assertEqual(result["items"][0]["status"], "needs_review")
                    self.assertFalse(result["items"][0]["can_resume"])
                    self.assertEqual(sent_registry.sent_emails("task", "clip"), set())

    def test_invalid_boolean_flags_cannot_enable_pending_resume(self):
        for flag in ("evaluation_required", "register_first", "suppress_per_chunk_catbear"):
            with self.subTest(flag=flag):
                self.save({**self.row, "state": "completing", "finalized": False, flag: "false"})
                self.assertFalse(recovery.snapshot()["items"][0]["can_resume"])
                with patch.object(upload, "pump_pending") as pump:
                    with self.assertRaises(ValueError):
                        recovery.resume_account("one@example.com", lambda email: "org")
                pump.assert_not_called()

    def test_invalid_state_or_phase_types_require_review_without_crashing(self):
        for field in ("state", "phase"):
            for value in ([], {}, 1, True, None):
                with self.subTest(field=field, value=value):
                    self.save({**self.row, field: value})
                    result = recovery.reconcile_confirmed()
                    self.assertEqual(result["reconciled"], 0)
                    self.assertEqual(result["items"][0]["status"], "needs_review")
                    self.assertFalse(result["items"][0]["can_resume"])
                    self.assertEqual(sent_registry.sent_emails("task", "clip"), set())

    def test_unknown_state_label_stays_visible_for_review(self):
        self.save({**self.row, "state": "unknown-state"})
        result = recovery.reconcile_confirmed()
        self.assertEqual(result["reconciled"], 0)
        self.assertEqual(result["items"][0]["status"], "needs_review")
        self.assertFalse(result["items"][0]["can_resume"])

    def test_every_chunk_count_must_be_an_integer_not_equal_float(self):
        for state in ("done", "completing"):
            with self.subTest(state=state):
                self.save({**self.row, "expected_chunk_count": 2, "state": state,
                           "finalized": state == "done"})
                self.save({**self.row, "expected_chunk_count": 2.0, "chunk_index": 1,
                           "state": state, "finalized": state == "done"}, "session1__1.json")
                result = recovery.reconcile_confirmed()
                self.assertEqual(result["reconciled"], 0)
                self.assertEqual(result["items"][0]["status"], "needs_review")
                self.assertFalse(result["items"][0]["can_resume"])
                self.assertEqual(sent_registry.sent_emails("task", "clip"), set())

    def test_boolean_chunk_index_is_not_a_complete_group(self):
        rows = [{**self.row, "expected_chunk_count": 2},
                {**self.row, "expected_chunk_count": 2, "chunk_index": True}]
        item = recovery._describe(rows, reset_checker=lambda *args: False)
        self.assertEqual(item["status"], "needs_review")
        self.assertFalse(item["can_resume"])

    def test_done_chunks_resume_only_evaluation_and_finalize(self):
        self.save({**self.row, "schema_version": 2, "phase": "done",
                   "finalized": False, "finalize_requested": True, "upload_id": "existing-upload",
                   "evaluation_required": True, "evaluation_verified": False})
        item = recovery.snapshot()["items"][0]
        self.assertEqual(item["status"], "pending")
        self.assertTrue(item["can_resume"])
        session = Mock(email="one@example.com")
        with patch.object(recovery.campaign.Session, "from_email", return_value=session), \
             patch.object(upload, "evaluate_upload", return_value={"checks": [{"status": "pass"}]}) as evaluation, \
             patch.object(upload, "_finalize_session", return_value=(True, 204)) as finalize, \
             patch.object(upload, "complete_upload") as complete, \
             patch.object(upload, "upload_session") as new_upload:
            result = recovery.resume_account("one@example.com", lambda email: "org")
        evaluation.assert_called_once_with(session, "existing-upload")
        finalize.assert_called_once_with(session, "org", "session1", 1)
        complete.assert_not_called()
        new_upload.assert_not_called()
        self.assert_history_pending(result)
        self.assertEqual(sent_registry.sent_emails("task", "clip"), {"one@example.com"})

    def test_resume_finalizes_existing_session_without_new_upload(self):
        self.save({**self.row, "state": "completing", "phase": "awaiting_finalize",
                   "finalized": False, "finalize_requested": True, "upload_id": "existing-upload"})
        session = Mock(email="one@example.com")
        with patch.object(recovery.campaign.Session, "from_email", return_value=session), \
             patch.object(upload, "evaluate_upload", return_value={"checks": [{"status": "pass"}]}), \
             patch.object(upload, "_finalize_session", return_value=(True, 200)) as finalize, \
             patch.object(upload, "upload_session") as new_upload:
            result = recovery.resume_account("one@example.com", lambda email: "org")
        finalize.assert_called_once_with(session, "org", "session1", 1)
        new_upload.assert_not_called()
        self.assert_history_pending(result)
        self.assertEqual(sent_registry.sent_emails("task", "clip"), {"one@example.com"})

    def test_resume_rejects_changed_organization_before_upload(self):
        self.save({**self.row, "state": "completing", "phase": "awaiting_finalize",
                   "upload_id": "existing-upload", "finalize_requested": True, "finalized": False})
        with patch.object(upload, "pump_pending") as pump:
            with self.assertRaises(ValueError):
                recovery.resume_account("one@example.com", lambda email: "different-org")
        pump.assert_not_called()

    def test_resume_targets_only_eligible_sessions_of_selected_account(self):
        self.save({**self.row, "state": "completing", "phase": "awaiting_finalize",
                   "upload_id": "existing-upload", "finalize_requested": True, "finalized": False})
        self.save({**self.row, "account_email": "other@example.com", "session_id": "session2",
                   "state": "completing", "phase": "awaiting_finalize", "upload_id": "other-upload",
                   "finalize_requested": True, "finalized": False}, "session2.json")
        with patch.object(recovery.campaign.Session, "from_email", return_value=Mock()), \
             patch.object(upload, "pump_pending") as pump:
            recovery.resume_account("one@example.com", lambda email: "org")
        self.assertEqual(pump.call_args.kwargs["session_ids"], {"session1"})
        self.assertEqual(pump.call_args.kwargs["account_email"], "one@example.com")

    def test_worker_keeps_failure_private_and_leaves_journals(self):
        self.save({**self.row, "state": "completing", "finalized": False})
        worker = recovery.RecoveryRunner()
        with patch.object(recovery, "resume_account", side_effect=RuntimeError("private-password")):
            worker.start("one@example.com", lambda email: "org")
            worker._thread.join(2)
        self.assertFalse(worker.running)
        self.assertEqual(worker.snapshot()["state"], "error")
        self.assertNotIn("private-password", json.dumps(worker.snapshot()))
        self.assertEqual(recovery.snapshot()["pending"], 1)

    def test_api_resume_requires_confirmation_and_known_account(self):
        from moneymin.web import server
        self.save({**self.row, "state": "completing", "phase": "awaiting_finalize",
                   "upload_id": "existing-upload", "finalize_requested": True, "finalized": False})
        worker = Mock(running=False)
        worker.snapshot.return_value = {"state": "running"}
        with patch.object(server, "RECOVERY", worker), \
             patch.object(server, "RUNNER", SimpleNamespace(running=False)), \
             patch.object(server, "HOLO_CACHE_RUNNER", SimpleNamespace(running=False)), \
             patch.object(server, "_list_accounts", return_value=[{"email": "one@example.com"}]):
            client = server.create_app(for_testing=True).test_client()
            self.assertEqual(client.post("/api/recovery/resume", json={"email": "one@example.com"}).status_code, 400)
            self.assertEqual(client.post("/api/recovery/resume", json={"email": "unknown@example.com", "confirmed": True}).status_code, 400)
            worker.start.assert_not_called()
            self.assertEqual(client.post("/api/recovery/resume", json={"email": "one@example.com", "confirmed": True}).status_code, 202)
        worker.start.assert_called_once_with("one@example.com", server._resolve_org)

    def test_active_recovery_protects_media_and_reset(self):
        from moneymin.web import server
        with patch.object(server, "RECOVERY", SimpleNamespace(running=True)), \
             patch.object(server, "RUNNER", SimpleNamespace(running=False)), \
             patch.object(server, "HOLO_CACHE_RUNNER", SimpleNamespace(running=False)), \
             patch.object(server.campaign, "cleanup_media_cache") as cleanup, \
             patch.object(sent_registry, "reset") as reset:
            client = server.create_app(for_testing=True).test_client()
            self.assertEqual(client.post("/api/storage/cleanup", json={}).status_code, 409)
            self.assertEqual(client.post("/api/sent/reset", json={}).status_code, 409)
        cleanup.assert_not_called()
        reset.assert_not_called()
