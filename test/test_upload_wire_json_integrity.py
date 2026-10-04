"""Success acknowledgments must be finite and unambiguous before advancing."""
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from moneymin import upload
import test_upload_response_contract as fixtures


class UploadWireJsonIntegrityTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.UploadResponseContractTests('test_new_upload_defaults_register_before_sas_and_transport')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.canary = 'PRIVATE_RAW_RESPONSE_CANARY'

    def test_create_rejects_ambiguous_or_nonfinite_ack_before_sas_and_put(self):
        payloads = ['{"id":"first","id":"fixture-upload"}',
                    '{"id":"fixture-upload","extra":NaN}',
                    '{"id":"fixture-upload","extra":1e400}',
                    '{"id":"fixture-upload","extra":{"value":1,"value":2}}']
        for raw in payloads:
            with self.subTest(raw=raw):
                session = fixtures.FakeSession(raw_create=raw)
                result = self.fixture.run_chunk(session)
                self.assertEqual(result.state, upload.STATE_FAILED)
                self.assertEqual(session.events, ['create'])
                self.fixture.video_put.assert_not_called()
                self.assertEqual(sum(path.startswith('/api/v1/uploads?') for _, path, _ in session.calls), 1)

    def test_sas_rejects_ambiguous_or_nonfinite_urls_before_transport(self):
        base = '{"signed_urls":[{"filename":"fixture-session_0.mp4","blob_url":"https://blob.invalid/video"}]'
        raws = [base + ',"extra":Infinity}', base + ',"extra":1e400}',
                '{"signed_urls":[{"filename":"different.mp4","filename":"fixture-session_0.mp4","blob_url":"https://blob.invalid/video"}]}']
        for raw in raws:
            with self.subTest(raw=raw):
                session = fixtures.FakeSession(raw_sas=raw)
                result = self.fixture.run_chunk(session)
                self.assertEqual(result.state, upload.STATE_FAILED)
                self.assertEqual(result.upload_id, 'fixture-upload')
                self.assertEqual(session.events, ['create', 'sas'])
                self.fixture.video_put.assert_not_called()

    def test_complete_rejects_ambiguous_or_nonfinite_success_for_review(self):
        for raw in ['{"status":"pending","status":"completed"}', '{"extra":NaN}',
                    '{"extra":1e400}', '[]', '{']:
            with self.subTest(raw=raw):
                session = Mock(spec=['request'])
                session.request.return_value = (200, raw)
                with self.assertRaises(upload.UploadError) as caught:
                    upload.complete_upload(session, 'fixture-upload', 21)
                self.assertIs(caught.exception.review_required, True)
                self.assertEqual(caught.exception.phase, 'complete')
                self.assertNotIn(self.canary, str(caught.exception))
                session.request.assert_called_once()

    def test_complete_ambiguous_ack_preserves_receipt_without_marking_remote_failed(self):
        session = fixtures.FakeSession()
        real_request = session.request
        def request(method, path, body=None):
            if path.endswith('/complete'):
                session.calls.append((method, path, body))
                session.events.append('complete')
                return 200, '{"status":"pending","status":"completed"}'
            return real_request(method, path, body)
        session.request = request
        result = self.fixture.run_chunk(session, fail_on_error=True)
        self.assertEqual(result.state, upload.STATE_QUARANTINE)
        self.assertEqual(result.upload_id, 'fixture-upload')
        self.assertEqual(self.fixture.checkpoint.call_args.kwargs['phase'], 'completion_review')
        self.assertNotIn('fail', session.events)
        self.assertEqual(session.events, ['create', 'sas', 'put-video', 'complete'])

    def test_evaluation_cannot_turn_duplicate_failed_check_into_a_pass(self):
        raws = ['{"upload_id":"fixture-upload","checks":[{"status":"fail","status":"pass"}]}',
                '{"upload_id":"other","upload_id":"fixture-upload","checks":[{"status":"pass"}]}',
                '{"upload_id":"fixture-upload","checks":[{"status":"pass"}],"extra":NaN}',
                '{"upload_id":"fixture-upload","checks":[{"status":"pass"}],"extra":1e400}']
        for raw in raws:
            with self.subTest(raw=raw):
                session = Mock(spec=['request'])
                session.request.return_value = (200, raw)
                with self.assertRaises(upload.UploadError):
                    upload.evaluate_upload(session, 'fixture-upload')
                session.request.assert_called_once()

    def test_query_rejects_ambiguous_or_nonfinite_receipt_states(self):
        for raw in ['{"status":"initiated","status":"completed"}',
                    '{"id":"fixture-upload","value":NaN}',
                    '{"id":"fixture-upload","value":1e400}']:
            with self.subTest(raw=raw):
                session = Mock(spec=['request'])
                session.request.return_value = (200, raw)
                with self.assertRaises(upload.UploadError) as caught:
                    upload.get_upload(session, 'fixture-upload')
                self.assertIs(caught.exception.review_required, True)

    def test_complete_conflict_does_not_accept_an_ambiguous_status_query(self):
        session = Mock(spec=['request'])
        session.request.side_effect = [(409, '{}'),
            (200, '{"status":"initiated","status":"completed"}')]
        with self.assertRaises(upload.UploadError) as caught:
            upload.complete_upload(session, 'fixture-upload', 21)
        self.assertIs(caught.exception.review_required, True)
        self.assertEqual(caught.exception.phase, 'complete')
        self.assertEqual(session.request.call_count, 2)

    def test_receipt_responses_cannot_switch_known_upload_identity(self):
        for route in ('get', 'complete'):
            for body in ({'id': 'other'}, {'uploadId': 'other'},
                         {'id': 'fixture-upload', 'upload_id': 'other'}, {'id': True}):
                with self.subTest(route=route, body=body):
                    session = Mock(spec=['request'])
                    session.request.return_value = (200, json.dumps(body))
                    with self.assertRaises(upload.UploadError) as caught:
                        if route == 'get':
                            upload.get_upload(session, 'fixture-upload')
                        else:
                            upload.complete_upload(session, 'fixture-upload', 21)
                    self.assertIs(caught.exception.review_required, True)
                    session.request.assert_called_once()

    def test_complete_conflict_with_unavailable_query_stays_retryable_without_remote_fail(self):
        session = fixtures.FakeSession()
        real_request = session.request
        def request(method, path, body=None):
            if path.endswith('/complete'):
                session.events.append('complete')
                return 409, '{}'
            if method == 'GET':
                session.events.append('query')
                return 503, '{}'
            return real_request(method, path, body)
        session.request = request
        result = self.fixture.run_chunk(session, fail_on_error=True)
        self.assertEqual(result.state, upload.STATE_COMPLETING)
        self.assertEqual(result.upload_id, 'fixture-upload')
        self.assertNotIn('fail', session.events)
        self.assertEqual(session.events, ['create', 'sas', 'put-video', 'complete', 'query'])

    def test_resume_ambiguous_complete_keeps_receipt_and_blocks_further_effects(self):
        row = {'session_id': 'fixture-session', 'chunk_index': 0, 'org_key': 'fixture-org',
               'account_email': 'fixture@example.invalid', 'upload_id': 'fixture-upload',
               'state': upload.STATE_COMPLETING, 'phase': 'transport_done', 'size_bytes': 21,
               'recorded_at': '2026-10-03T00:00:00.000Z', 'crash_resumes': 0,
               'expected_chunk_count': 1, 'finalize_requested': True, 'finalized': False,
               'suppress_per_chunk_catbear': True}
        session = Mock(spec=['request'])
        session.request.return_value = (200, '{"status":"pending","status":"completed"}')
        with patch.object(upload, 'list_sidecars', side_effect=lambda: [row]), \
                patch.object(upload, 'save_sidecar'), \
                patch.object(upload, 'load_sidecar', side_effect=lambda *_args: dict(row)), \
                patch.object(upload, '_remove_sidecar_archive') as remove_archive:
            result = upload.pump_pending(session, account_email='fixture@example.invalid', max_retries=1)
            self.assertEqual(result[0]['state'], upload.STATE_QUARANTINE)
            self.assertEqual(result[0]['phase'], 'completion_review')
            self.assertEqual(result[0]['upload_id'], 'fixture-upload')
            self.assertIs(result[0]['finalized'], False)
            session.request.assert_called_once()
            remove_archive.assert_not_called()
            snapshot = dict(row)
            self.assertEqual(upload.pump_pending(session, account_email='fixture@example.invalid'), [])
            self.assertEqual(row, snapshot)
            with self.assertRaises(upload.UploadError):
                upload._pending_recovery_stage(row)
            session.request.assert_called_once()

    def test_full_quality_flow_does_not_finalize_or_delete_an_ambiguous_evaluation(self):
        session = fixtures.FakeSession()
        real_request = session.request
        def request(method, path, body=None):
            if path.endswith('/evaluate'):
                session.events.append('evaluate')
                return 200, '{"upload_id":"fixture-upload","checks":[{"status":"fail","status":"pass"}]}'
            if path.endswith('/finalize'):
                session.events.append('finalize')
                return 204, ''
            return real_request(method, path, body)
        session.request = request
        fixture_now = datetime(2026, 10, 3, 0, 2, tzinfo=timezone.utc).timestamp()
        with patch.object(upload.time, 'time', return_value=fixture_now):
            result = upload.upload_session(session, self.fixture.video, 'fixture-org',
                session_id='fixture-session', recorded_at='2026-10-03T00:00:00.000Z',
                normalize=False, sidecar=False, evaluate=True, finalize=True, persist_sidecar=False,
                device_meta={}, platform_meta={}, video_meta={}, network_meta={},
                max_retries=1, fail_on_error=False)
        self.assertIs(result.finalized, False)
        self.assertNotIn('finalize', session.events)
        self.assertNotIn('fail', session.events)

    def test_valid_successes_and_empty_complete_ack_keep_the_existing_contract(self):
        session = fixtures.FakeSession()
        result = self.fixture.run_chunk(session)
        self.assertEqual(result.state, upload.STATE_DONE)
        for code, text, expected in [(204, '', {}), (200, '{}', {}), (200, '{"id":"fixture-upload"}', {'id': 'fixture-upload'})]:
            with self.subTest(status=code):
                session = Mock(spec=['request'])
                session.request.return_value = (code, text)
                self.assertEqual(upload.complete_upload(session, 'fixture-upload', 21), expected)
        session = Mock(spec=['request'])
        evaluation = {'upload_id': 'fixture-upload', 'checks': [{'id': 'fixture', 'label': 'Declared inert quality', 'status': 'pass', 'detail': None}]}
        session.request.return_value = (200, json.dumps(evaluation))
        self.assertEqual(upload.evaluate_upload(session, 'fixture-upload'), evaluation)
