import json
import tempfile
import unittest
import threading
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from moneymin import config, recovery, sent_registry, upload
from moneymin.media_lifecycle import media_state_lease
from moneymin.operation_lease import OperationLeaseError
from moneymin.recovery_errors import RecoveryReadError, error_response


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
                       patch("moneymin.registration_proxy.assign", return_value=None),
                       patch("moneymin.registration_proxy.route", side_effect=lambda proxy: nullcontext(proxy)),
                       patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))):
            mocked.start()
            self.addCleanup(mocked.stop)
        self.row = {"account_email": "one@example.com", "org_key": "org", "session_id": "session1",
                    "chunk_index": 0, "expected_chunk_count": 1, "task_id": "task",
                    "upload_id": "accepted-session1-upload", "state": "done", "finalized": True,
                    "campaign_context": {"registry_key": "task", "clip_uid": "clip", "task_id": "task"}}

    def save(self, row=None, name="session1.json"):
        (self.journals / name).write_text(json.dumps(row or self.row), encoding="utf-8")

    def test_legacy_transport_outage_offers_recovery_but_quality_rejection_does_not(self):
        row = {**self.row, "state": "quarantine", "phase": "evaluation_review",
               "finalized": False, "finalize_requested": True,
               "evaluation_required": True, "evaluation_verified": False,
               "error": "Avaliação inconclusiva (HTTP -1); envio preservado para revisão."}
        self.save(row)
        self.assertTrue(recovery.snapshot()['items'][0]['can_resume'])
        self.save({**row, "error": "Avaliação reprovada: quality."})
        self.assertFalse(recovery.snapshot()['items'][0]['can_resume'])

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

    def test_targeted_resume_describes_only_requested_session_in_large_store(self):
        for index in range(200):
            row = {**self.row, "account_email": "other@example.com",
                   "session_id": f"old-session-{index}", "campaign_reconciled": True}
            row.pop("campaign_context")
            self.save(row, name=f"old-session-{index}.json")
        target = {**self.row, "session_id": "target-session",
                  "state": "quarantine", "phase": "evaluation_review",
                  "finalize_requested": True, "finalized": False,
                  "evaluation_required": True, "evaluation_verified": False,
                  "evaluation_http_status": -1}
        self.save(target, name="target-session.json")

        session = Mock()
        with patch.object(recovery, "_describe", wraps=recovery._describe) as describe, \
             patch.object(recovery.campaign.Session, "from_email", return_value=session), \
             patch.object(upload, "pump_pending") as pump, \
             patch.object(recovery, "reconcile_confirmed", return_value={"resumed": True}), \
             patch.object(recovery.campaign, "_legacy_upload_contexts",
                          wraps=recovery.campaign._legacy_upload_contexts) as legacy_contexts:
            result = recovery.resume_account(
                self.row["account_email"], lambda _email: self.row["org_key"],
                session_id="target-session")

        self.assertEqual(result, {"resumed": True})
        self.assertEqual(describe.call_count, 1)
        self.assertEqual(describe.call_args.args[0][0]["session_id"], "target-session")
        legacy_contexts.assert_not_called()
        pump.assert_called_once()
        self.assertEqual(pump.call_args.kwargs["session_ids"], {"target-session"})

    def test_account_resume_batches_selected_legacy_contexts_with_full_journal_evidence(self):
        rows = []
        accounts = []
        for email, sid in (("one@example.com", "legacy-one"),
                           ("one@example.com", "legacy-two"),
                           ("other@example.com", "unrelated-legacy")):
            row = {**self.row, "account_email": email, "session_id": sid,
                   "state": "quarantine", "phase": "evaluation_review",
                   "finalize_requested": True, "finalized": False,
                   "evaluation_required": True, "evaluation_verified": False,
                   "evaluation_http_status": -1}
            row.pop("campaign_context")
            self.save(row, name=f"{sid}.json")
            rows.append(row)
            accounts.append({"session_id": sid, "email": email})
        (self.root / "campaign_legacy.json").write_text(json.dumps({"items": [{
            "clip_uid": "clip", "registry_key": "task", "task_id": "task",
            "accounts": accounts}]}), encoding="utf-8")

        session = Mock()
        with patch.object(recovery.campaign.Session, "from_email", return_value=session), \
             patch.object(upload, "pump_pending") as pump, \
             patch.object(recovery, "reconcile_confirmed", return_value={"resumed": True}), \
             patch.object(recovery.campaign, "_legacy_upload_contexts",
                          wraps=recovery.campaign._legacy_upload_contexts) as legacy_contexts:
            result = recovery.resume_account(
                "one@example.com", lambda _email: "org")

        self.assertEqual(result, {"resumed": True})
        legacy_contexts.assert_called_once()
        wanted, evidence = legacy_contexts.call_args.args
        self.assertEqual(wanted, {("legacy-one", "one@example.com"),
                                  ("legacy-two", "one@example.com")})
        self.assertEqual({row["session_id"] for row in evidence},
                         {"legacy-one", "legacy-two", "unrelated-legacy"})
        self.assertEqual(pump.call_args.kwargs["session_ids"],
                         {"legacy-one", "legacy-two"})

    def test_targeted_resume_still_fails_closed_on_unrelated_corruption_or_sid_conflict(self):
        target = {**self.row, "state": "quarantine", "phase": "evaluation_review",
                  "finalize_requested": True, "finalized": False,
                  "evaluation_required": True, "evaluation_verified": False,
                  "evaluation_http_status": -1}
        self.save(target)
        with patch.object(recovery.campaign.Session, "from_email") as from_email, \
             patch.object(upload, "pump_pending") as pump:
            broken = self.journals / "unrelated.json"
            broken.write_text("{", encoding="utf-8")
            with self.assertRaises(RecoveryReadError):
                recovery.resume_account("one@example.com", lambda _email: "org",
                                        session_id="session1")
            broken.unlink()

            self.save({**target, "account_email": "other@example.com", "chunk_index": 1,
                       "expected_chunk_count": 2}, "session1__1.json")
            with self.assertRaises(RecoveryReadError):
                recovery.resume_account("one@example.com", lambda _email: "org",
                                        session_id="session1")
        from_email.assert_not_called()
        pump.assert_not_called()

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

    def test_history_reset_preserves_interrupted_sessions_and_their_reservations(self):
        reset_path = self.root / "sent_reset_history.json"
        reset_path.write_text(json.dumps({"all": ["campaign_old.json"],
                                         "completed_sessions": ["session1"]}), encoding="utf-8")
        context = {**self.row["campaign_context"], "history_name": "campaign_old.json"}
        for phase, state, receipt in (("queued", "creating", None),
                                      ("sas_ready", "transport", "accepted-session1-upload"),
                                      ("transport_done", "completing", "accepted-session1-upload")):
            with self.subTest(phase=phase):
                row = {**self.row, "campaign_context": context, "phase": phase,
                       "state": state, "finalized": False, "upload_id": receipt,
                       "create_attempted": receipt is not None,
                       "recorded_at": "2026-10-06T15:30:00Z"}
                self.save(row)
                before = (self.journals / "session1.json").read_bytes()
                result = recovery.snapshot()
                self.assertEqual(result["pending"], 1)
                self.assertEqual(recovery.campaign_exclusions(result["items"]),
                                 {"clip": ["one@example.com"]})
                self.assertEqual((self.journals / "session1.json").read_bytes(), before)

    def test_explicit_completed_session_reset_survives_archived_legacy_history(self):
        row = {**self.row, "campaign_context": None}
        self.save(row)
        before = (self.journals / "session1.json").read_bytes()
        (self.root / "sent_reset_history.json").write_text(
            json.dumps({"completed_sessions": ["session1"]}), encoding="utf-8")
        self.assertEqual(recovery.snapshot()["items"], [])
        self.assertEqual((self.journals / "session1.json").read_bytes(), before)

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

    def test_corrupt_record_diagnostic_is_stable_private_and_preserves_bytes(self):
        path = self.journals / "secret-account-token.json"
        path.write_bytes(b"{private-payload")
        with self.assertRaises(RecoveryReadError) as caught:
            recovery.snapshot()
        first = error_response(caught.exception)
        self.assertEqual(first["recovery_error"]["code"], "journal_unreadable")
        self.assertEqual(len(first["recovery_error"]["record_ref"]), 12)
        with self.assertRaises(RecoveryReadError) as repeated:
            recovery.snapshot()
        self.assertEqual(first, error_response(repeated.exception))
        self.assertNotIn("secret-account-token", json.dumps(first))
        self.assertNotIn("private-payload", json.dumps(first))
        self.assertNotIn(str(self.root), json.dumps(first))
        self.assertEqual(path.read_bytes(), b"{private-payload")

    def test_reset_history_has_specific_diagnostic_and_still_blocks(self):
        self.save()
        reset = self.root / "sent_reset_history.json"
        reset.write_bytes(b"{invalid-private-value")
        with self.assertRaises(RecoveryReadError) as caught:
            recovery.snapshot()
        self.assertEqual(caught.exception.code, "reset_history")
        self.assertEqual(reset.read_bytes(), b"{invalid-private-value")
        self.assertEqual(json.loads((self.journals / "session1.json").read_text()), self.row)

    def test_snapshot_holds_cleanup_barrier_while_reading_journal(self):
        self.save()
        results = []
        original = upload._read_sidecar_file
        def read(path):
            def competing_cleanup():
                try:
                    with media_state_lease():
                        results.append("unsafe")
                except OperationLeaseError:
                    results.append("blocked")
            thread = threading.Thread(target=competing_cleanup)
            thread.start()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            return original(path)
        with patch.object(upload, "_read_sidecar_file", side_effect=read):
            self.assertEqual(recovery.snapshot()["confirmed"], 1)
        self.assertEqual(results, ["blocked"])

    def test_busy_snapshot_reports_retry_without_reading_or_empty_result(self):
        with patch.object(recovery, "media_state_lease", side_effect=OperationLeaseError("private")), \
             patch.object(recovery, "_groups") as groups:
            with self.assertRaises(RecoveryReadError) as caught:
                recovery.snapshot()
        self.assertEqual(caught.exception.code, "busy")
        groups.assert_not_called()
        self.assertNotIn("private", json.dumps(error_response(caught.exception)))

    def test_history_inspection_does_not_block_checkpoint_writers(self):
        self.save()
        original = recovery._read_required_publications
        results = []
        def read(groups):
            def checkpoint():
                try:
                    with media_state_lease():
                        results.append("available")
                except OperationLeaseError:
                    results.append("blocked")
            thread = threading.Thread(target=checkpoint)
            thread.start()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            return original(groups)
        with patch.object(recovery, "_read_required_publications", side_effect=read):
            self.assertEqual(recovery.snapshot()["confirmed"], 1)
        self.assertEqual(results, ["available"])

    def test_api_exposes_specific_diagnostic_for_sync_and_async(self):
        from moneymin.web import server
        client = server.create_app(for_testing=True).test_client()
        failure = RecoveryReadError("journal_conflict", Path("secret-session.json"))
        with patch.object(recovery, "snapshot", side_effect=failure):
            expected = error_response(failure)
            response = client.get("/api/recovery")
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.get_json()["recovery_error"], expected["recovery_error"])
            # The loader callback runs in-process so this checks mapping, not timing.
            from moneymin.web.catalog_loader import CatalogLoader
            with patch.object(CatalogLoader, "get", side_effect=lambda key, work, **kwargs: work(lambda text: None)):
                response = client.get("/api/recovery?async=1")
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.get_json()["recovery_error"], expected["recovery_error"])
            self.assertNotIn("secret-session", response.get_data(as_text=True))

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

    def test_selected_session_does_not_resume_other_old_sessions_of_same_account(self):
        pending = {**self.row, 'state': 'completing', 'phase': 'awaiting_finalize',
                   'finalized': False, 'finalize_requested': True}
        self.save(pending)
        self.save({**pending, 'session_id': 'old-session', 'upload_id': 'old-receipt'}, 'old-session.json')
        before = (self.journals / 'old-session.json').read_bytes()
        with patch.object(recovery.campaign.Session, 'from_email', return_value=Mock()), \
             patch.object(upload, 'pump_pending') as pump:
            recovery.resume_account('one@example.com', lambda email: 'org', session_id='session1')
        self.assertEqual(pump.call_args.kwargs['session_ids'], {'session1'})
        self.assertEqual((self.journals / 'old-session.json').read_bytes(), before)

    def test_resume_uses_assigned_identity_proxy_for_org_auth_and_pending_uploads(self):
        self.save({**self.row, "state": "completing", "phase": "awaiting_finalize",
                   "upload_id": "existing-upload", "finalized": False,
                   "finalize_requested": True})
        assigned = {"id": "fixture-proxy", "host": "proxy.invalid", "port": 1234,
                    "username": "fixture-user", "password": "fixture-secret"}
        active = False

        @contextmanager
        def routed(proxy):
            nonlocal active
            self.assertIs(proxy, assigned)
            active = True
            try:
                yield
            finally:
                active = False

        def resolve(_email):
            self.assertTrue(active, "organization resolution escaped the assigned route")
            return "org"

        session = Mock()
        def ensure_auth(*, org_key):
            self.assertTrue(active, "Minute authentication escaped the assigned route")
            self.assertEqual(org_key, "org")

        session.ensure_auth.side_effect = ensure_auth
        def pump(*_args, **_kwargs):
            self.assertTrue(active, "pending upload escaped the assigned route")
            return []

        with patch("moneymin.registration_proxy.assign", return_value=assigned) as assign, \
             patch("moneymin.registration_proxy.route", side_effect=routed) as route, \
             patch.object(recovery.campaign.Session, "from_email", return_value=session), \
             patch.object(upload, "pump_pending", side_effect=pump) as pump_pending, \
             patch.object(recovery, "reconcile_confirmed", return_value={"items": []}):
            recovery.resume_account("one@example.com", resolve)

        assign.assert_called_once_with("one@example.com", "")
        route.assert_called_once_with(assigned)
        self.assertEqual(pump_pending.call_args.kwargs["session_ids"], {"session1"})
        self.assertFalse(active)

    def test_resume_does_not_fall_back_to_direct_when_assigned_route_fails(self):
        self.save({**self.row, "state": "completing", "phase": "awaiting_finalize",
                   "upload_id": "existing-upload", "finalized": False,
                   "finalize_requested": True})
        with patch("moneymin.registration_proxy.assign", return_value={"id": "assigned"}), \
             patch("moneymin.registration_proxy.route", side_effect=RuntimeError("proxy-secret-url")), \
             patch.object(recovery.campaign.Session, "from_email") as from_email, \
             patch.object(upload, "pump_pending") as pump:
            with self.assertRaisesRegex(RuntimeError, "proxy-secret-url"):
                recovery.resume_account("one@example.com", Mock())
        from_email.assert_not_called()
        pump.assert_not_called()

    def test_worker_reports_safe_auth_stage_and_category_without_exception_text(self):
        from moneymin.minute_api import AuthError

        def fail_at_auth(_email, _resolve, **kwargs):
            kwargs["on_stage"]("minute_authentication")
            raise AuthError("private-token-and-signed-url", code="service")

        worker = recovery.RecoveryRunner()
        with patch.object(recovery, "resume_account", side_effect=fail_at_auth):
            worker._run("one@example.com", lambda _email: "org")
        state = worker.snapshot()
        self.assertEqual(state["state"], "error")
        self.assertEqual(state["error_stage"], "minute_authentication")
        self.assertEqual(state["error_category"], "minute_service")
        self.assertIn("autenticação Minute", state["error"])
        self.assertNotIn("private-token-and-signed-url", json.dumps(state))

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
            worker.start.reset_mock()
            for sid in ('../foreign', True, None, '', 'unknown-session'):
                response = client.post('/api/recovery/resume', json={
                    'email': 'one@example.com', 'confirmed': True, 'session_id': sid})
                self.assertIn(response.status_code, (400, 409))
            worker.start.assert_not_called()
            self.assertEqual(client.post('/api/recovery/resume', json={
                'email': 'one@example.com', 'confirmed': True, 'session_id': 'session1'}).status_code, 202)
        worker.start.assert_called_once_with("one@example.com", server._resolve_org, session_id='session1')

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
