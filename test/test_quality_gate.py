import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from moneymin import upload
from moneymin.minute_api import _auth_failure
from moneymin.web.account_issues import account_issue


class EvaluationContractTests(unittest.TestCase):
    def evaluation(self, status="pass", uid="u"):
        return {"upload_id": uid, "checks": [{"id": "quality", "label": "Quality", "status": status}]}

    def test_missing_pending_and_malformed_checks_are_not_perfect(self):
        for payload in ({}, None, [], {"error": "timeout"}, {"checks": []},
                        {"checks": [{"status": "pending"}]}, {"checks": [None]},
                        {"checks": [{"status": []}]}):
            with self.subTest(payload=payload):
                self.assertFalse(upload.is_perfect(payload))

    def test_completed_checks_keep_pass_fail_skip_contract(self):
        self.assertTrue(upload.is_perfect(self.evaluation()))
        self.assertTrue(upload.is_perfect(self.evaluation("skip")))
        self.assertFalse(upload.is_perfect(self.evaluation("fail")))

    def test_response_must_belong_to_requested_upload(self):
        session = Mock()
        for payload in ({}, None, [], self.evaluation(uid="other"),
                        {"upload_id": "u", "checks": []}, self.evaluation("pending")):
            with self.subTest(payload=payload):
                session.request.return_value = (200, json.dumps(payload))
                with self.assertRaises(upload.UploadError):
                    upload.evaluate_upload(session, "u")
        session.request.return_value = (200, json.dumps(self.evaluation()))
        self.assertEqual(upload.evaluate_upload(session, "u"), self.evaluation())


class SessionQualityGateTests(unittest.TestCase):
    def run_upload(self, evaluation):
        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            paths = [Path(root) / "a.mp4", Path(root) / "b.mp4"]
            for path in paths:
                path.write_bytes(b"fixture")
            stack.enter_context(patch.object(upload, "_probe_duration_ms", return_value=1000))
            stack.enter_context(patch.object(upload, "_upload_single_chunk", side_effect=[
                upload.ChunkResult(f"u{i}", i, f"s_{i}", "blob", 7, 1000) for i in range(2)]))
            stack.enter_context(patch.object(upload, "evaluate_upload", side_effect=evaluation))
            stack.enter_context(patch.object(upload, "save_sidecar"))
            stack.enter_context(patch.object(upload, "load_sidecar", return_value={"session_id": "s"}))
            final = stack.enter_context(patch.object(upload, "_finalize_session", return_value=(True, 204)))
            remove = stack.enter_context(patch.object(upload, "_remove_sidecar_archive"))
            fail = stack.enter_context(patch.object(upload, "fail_upload"))
            delete = stack.enter_context(patch.object(upload, "delete_upload"))
            delete_session = stack.enter_context(patch.object(upload, "delete_session"))
            result = upload.upload_session(object(), paths, "org", session_id="s", evaluate=True,
                                           normalize=False, sidecar=False, persist_sidecar=True)
            return result, final.call_count, remove.call_count, fail.call_count + delete.call_count + delete_session.call_count

    def test_failed_check_prevents_finalize_and_preserves_uploads(self):
        result, final, removed, deleted = self.run_upload([
            {"checks": [{"status": "pass"}]}, {"checks": [{"id": "category", "status": "fail"}]}])
        self.assertFalse(result.finalized)
        self.assertEqual((final, removed, deleted), (0, 0, 0))
        self.assertIn("category", result.chunks[1].error)

    def test_empty_evaluation_prevents_finalize(self):
        result, final, removed, deleted = self.run_upload([{}, {"checks": [{"status": "pass"}]}])
        self.assertFalse(result.finalized)
        self.assertEqual((final, removed, deleted), (0, 0, 0))
        self.assertIn("inconclusiva", result.chunks[0].error)

    def test_evaluation_outage_is_not_an_approval(self):
        result, final, removed, deleted = self.run_upload([
            upload.UploadError("service unavailable"), {"checks": [{"status": "pass"}]}])
        self.assertFalse(result.finalized)
        self.assertEqual((final, removed, deleted), (0, 0, 0))

    def test_passed_evaluations_still_finalize(self):
        result, final, removed, deleted = self.run_upload([
            {"checks": [{"status": "pass"}]}, {"checks": [{"status": "skip"}]}])
        self.assertTrue(result.finalized)
        self.assertEqual((final, removed, deleted), (1, 2, 0))

    def test_automatic_recovery_does_not_finalize_quality_quarantine(self):
        journal = {"session_id": "s", "account_email": "a@example.com", "org_key": "org",
                   "chunk_index": 0, "expected_chunk_count": 1, "upload_id": "u",
                   "state": upload.STATE_QUARANTINE, "phase": "evaluation_review", "finalize_requested": True}
        with patch.object(upload, "list_sidecars", return_value=[journal]), \
             patch.object(upload, "_finalize_session") as final, \
             patch.object(upload, "upload_session") as send:
            self.assertEqual(upload.pump_pending(SimpleNamespace(email="a@example.com")), [])
            final.assert_not_called()
            send.assert_not_called()


class DisabledAccessDiagnosticTests(unittest.TestCase):
    def test_explicit_disabled_reply_is_distinct_from_generic_403(self):
        for body in ('{"detail":"User account is disabled."}', 'User account is disabled.'):
            result = account_issue("a@example.com", _auth_failure(403, body, "Profile"))
            self.assertEqual(result["code"], "restricted")
            self.assertTrue(result["restriction_confirmed"])
        for body in ('{"detail":"Forbidden"}', 'device disabled', 'unknown'):
            self.assertEqual(_auth_failure(403, body, "Profile").account_issue_code, "forbidden")
        self.assertEqual(_auth_failure(503, 'User account is disabled.', "Profile").account_issue_code, "service")
