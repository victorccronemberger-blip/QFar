import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from moneymin import upload, config
from moneymin.upload_types import is_pending_evaluation
from moneymin.minute_api import _auth_failure
from moneymin.web.account_issues import account_issue


class EvaluationContractTests(unittest.TestCase):
    def evaluation(self, status="pass", uid="u"):
        return {"upload_id": uid, "checks": [{"id": "quality", "label": "Quality", "status": status, "detail": None}]}

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

    def test_transport_outage_retries_only_the_existing_upload(self):
        session = Mock()
        session.request.side_effect = [(-1, "transport failure"),
                                       (503, "unavailable"),
                                       (200, json.dumps(self.evaluation()))]
        with patch.object(upload.time, "sleep") as sleep:
            self.assertEqual(upload.evaluate_upload(session, "u"), self.evaluation())
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(session.request.call_count, 3)
        for request in session.request.call_args_list:
            self.assertEqual(request.args, ("POST", "/api/v1/uploads/u/evaluate"))

    def test_persistent_outage_is_bounded_and_not_quality_approval(self):
        session = Mock()
        session.request.return_value = (-1, "transport failure")
        with patch.object(upload.time, "sleep") as sleep:
            with self.assertRaises(upload.UploadError) as raised:
                upload.evaluate_upload(session, "u")
        self.assertEqual(raised.exception.status_code, -1)
        self.assertEqual(raised.exception.attempts, 3)
        self.assertEqual(session.request.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_rejection_rate_limit_and_malformed_reply_are_not_retried(self):
        for response in [(401, "denied"), (429, "slow down"),
                         (200, "invalid"), (200, json.dumps(self.evaluation(uid="other")))]:
            with self.subTest(response=response):
                session = Mock()
                session.request.return_value = response
                with patch.object(upload.time, "sleep") as sleep:
                    with self.assertRaises(upload.UploadError):
                        upload.evaluate_upload(session, "u")
                self.assertEqual(session.request.call_count, 1)
                sleep.assert_not_called()


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
            # These are new sends, followed by stateful quality checkpoints.
            # An incomplete preexisting journal is no longer a valid fixture
            # for that flow: it must preserve its original recording time.
            journals = {}

            def save_journal(row):
                journals[(row["session_id"], row.get("chunk_index", 0))] = dict(row)

            def load_journal(sid, index=0):
                row = journals.get((sid, index))
                return dict(row) if row is not None else None

            stack.enter_context(patch.object(upload, "save_sidecar", side_effect=save_journal))
            stack.enter_context(patch.object(upload, "load_sidecar", side_effect=load_journal))
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

class RecoveryQualityGateTests(unittest.TestCase):
    def test_unavailable_evaluation_can_resume_but_rejection_and_ambiguous_journals_cannot(self):
        row = {'session_id': 's', 'account_email': 'a@example.com', 'org_key': 'org',
               'chunk_index': 0, 'expected_chunk_count': 1, 'upload_id': 'u',
               'state': upload.STATE_QUARANTINE, 'phase': 'evaluation_review',
               'finalize_requested': True, 'finalized': False,
               'evaluation_required': True, 'evaluation_verified': False,
               'error': 'Avaliação inconclusiva (HTTP -1); envio preservado para revisão.'}
        self.assertTrue(is_pending_evaluation(row))
        for changes in ({'error': 'Avaliação reprovada: quality.'}, {'error': 'HTTP -1'},
                        {'upload_id': ''}, {'upload_id': '../other'}, {'upload_id': 'u?secret'},
                        {'upload_id': 'u%2fother'}, {'upload_id': 'u\n'},
                        {'finalize_requested': False}, {'finalized': True},
                        {'evaluation_verified': 'false'}, {'phase': 'quality_rejected'},
                        {'evaluation_http_status': None}, {'evaluation_http_status': True},
                        {'evaluation_http_status': 400}, {'evaluation_http_status': '503'}):
            with self.subTest(changes=changes):
                self.assertFalse(is_pending_evaluation({**row, **changes}))

    def test_real_persisted_outage_resumes_evaluation_without_create_or_put(self):
        with tempfile.TemporaryDirectory(prefix='qmoney-evaluation-recovery-') as root, ExitStack() as stack:
            stack.enter_context(patch.object(config, 'DATA_DIR', Path(root) / 'state'))
            path = Path(root) / 'inert.mp4'
            path.write_bytes(b'fixture')
            session = SimpleNamespace(email='a@example.invalid', request=Mock(
                return_value=(-1, 'unavailable')))
            stack.enter_context(patch.object(upload, '_probe_duration_ms', return_value=1000))
            transport = stack.enter_context(patch.object(upload, '_upload_single_chunk', return_value=
                upload.ChunkResult('existing-receipt', 0, 'fixed-session_0', 'inert-blob', 7, 1000)))
            finalize = stack.enter_context(patch.object(upload, '_finalize_session', return_value=(True, 204)))
            stack.enter_context(patch.object(upload.time, 'sleep'))
            result = upload.upload_session(session, path, 'org', session_id='fixed-session',
                evaluate=True, normalize=False, sidecar=False, persist_sidecar=True)
            self.assertFalse(result.finalized)
            finalize.assert_not_called()
            saved = upload.load_sidecar('fixed-session', 0)
            self.assertTrue(is_pending_evaluation(saved))
            self.assertEqual(saved['evaluation_http_status'], -1)
            session.request.return_value = (200, json.dumps({'upload_id': 'existing-receipt',
                'checks': [{'id': 'quality', 'label': 'Quality', 'status': 'pass', 'detail': None}]}))
            upload.pump_pending(session, account_email=session.email, required_org_key='org',
                                session_ids={'fixed-session'})
            recovered = upload.load_sidecar('fixed-session', 0)
            self.assertTrue(upload.journal_delivery_confirmed(recovered))
            self.assertEqual(recovered['upload_id'], 'existing-receipt')
            self.assertEqual(recovered['session_id'], 'fixed-session')
            self.assertEqual(transport.call_count, 1)
            finalize.assert_called_once_with(session, 'org', 'fixed-session', 1)
            self.assertTrue(path.exists())
            for request in session.request.call_args_list:
                self.assertEqual(request.args, ('POST', '/api/v1/uploads/existing-receipt/evaluate'))

    def run_recovery(self, evaluation, phase='awaiting_finalize', required=True, verified=False):
        journal = {'session_id': 's', 'account_email': 'a@example.com', 'org_key': 'org',
                   'chunk_index': 0, 'expected_chunk_count': 1, 'upload_id': 'u',
                   'state': upload.STATE_COMPLETING, 'phase': phase, 'finalize_requested': True,
                   'evaluation_verified': verified,
                   'campaign_context': {'registry_key': 'task', 'clip_uid': 'clip'}}
        if required is not None:
            journal['evaluation_required'] = required
        with patch.object(upload, 'list_sidecars', return_value=[journal]), \
             patch.object(upload, 'save_sidecar'), \
             patch.object(upload, 'evaluate_upload', side_effect=evaluation) as evaluate, \
             patch.object(upload, '_finalize_session', return_value=(True, 204)) as finalize, \
             patch.object(upload, 'upload_session') as resend, \
             patch.object(upload, '_remove_sidecar_archive') as remove:
            upload.pump_pending(SimpleNamespace(email='a@example.com'))
            return journal, evaluate.call_count, finalize.call_count, resend.call_count, remove.call_count

    def test_recovery_evaluation_failure_keeps_original_upload_in_review(self):
        for reply in ({'checks': [{'status': 'fail'}]}, {}, upload.UploadError('HTTP 503')):
            with self.subTest(reply=reply):
                journal, evaluated, finalized, resent, removed = self.run_recovery([reply])
                self.assertEqual((evaluated, finalized, resent, removed), (1, 0, 0, 0))
                self.assertEqual(journal['state'], upload.STATE_QUARANTINE)
                self.assertEqual(journal['phase'], 'evaluation_review')
                self.assertEqual(journal['upload_id'], 'u')
                self.assertFalse(journal['finalized'])

    def test_crash_before_finalize_reuses_verified_evaluation_without_resending(self):
        journal, evaluated, finalized, resent, _ = self.run_recovery([], phase='finalizing', verified=True)
        self.assertEqual((evaluated, finalized, resent), (0, 1, 0))
        self.assertTrue(journal['finalized'])

    def test_legacy_campaign_journal_is_evaluated_before_finalize(self):
        journal, evaluated, finalized, resent, _ = self.run_recovery(
            [{'checks': [{'status': 'pass'}]}], required=None)
        self.assertEqual((evaluated, finalized, resent), (1, 1, 0))
        self.assertTrue(journal['evaluation_verified'])
        self.assertTrue(journal['finalized'])
