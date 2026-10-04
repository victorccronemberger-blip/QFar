"""Native128 response shapes, with inert media/provider and no real capture."""
from datetime import datetime, timezone
import copy
import json
import unittest
from unittest.mock import Mock, patch
from moneymin import upload, recovery
import test_upload_response_contract as fixtures


class NativeSession(fixtures.FakeSession):
    def __init__(self, *, create=None, complete=None, evaluation=None):
        super().__init__(create_body={'id': 'fixture-upload', 'status': 'initiated', 'meta': {}} if create is None else create)
        self.complete_body = {'id': 'fixture-upload', 'status': 'uploaded', 'meta': {}} if complete is None else complete
        self.evaluation = evaluation

    def request(self, method, path, body=None):
        if path.endswith('/complete'):
            self.calls.append((method, path, body))
            self.events.append('complete')
            return 200, json.dumps(self.complete_body)
        if path.endswith('/evaluate'):
            self.calls.append((method, path, body))
            self.events.append('evaluate')
            return 200, json.dumps(self.evaluation)
        if path.endswith('/finalize'):
            self.events.append('finalize')
            return 204, ''
        return super().request(method, path, body)


class NativeResponseSchemaTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.UploadResponseContractTests('test_new_upload_defaults_register_before_sas_and_transport')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.good_check = {'id': 'fixture-quality', 'label': 'Inert quality fixture', 'status': 'pass', 'detail': None}

    def malformed_checks(self):
        result = []
        for name in ('id', 'label', 'detail'):
            item = dict(self.good_check)
            del item[name]
            result.append(item)
        for name in ('id', 'label'):
            for value in (None, True, 1, [], {}):
                result.append({**self.good_check, name: value})
        for value in (True, 1, [], {}):
            result.append({**self.good_check, 'detail': value})
        return result

    def run_session(self, session, *, persist=False):
        now = datetime(2026, 10, 4, 7, 0, tzinfo=timezone.utc).timestamp()
        self.fixture.video_put.side_effect = lambda *_args, **_kwargs: session.events.append('put-video') or 201
        with patch.object(upload.time, 'time', return_value=now):
            return upload.upload_session(session, self.fixture.video, 'fixture-org',
                session_id='fixture-session', recorded_at='2026-10-04T06:55:00.000Z',
                normalize=False, sidecar=False, evaluate=True, finalize=True,
                persist_sidecar=persist, task_id='fixture-task',
                campaign_context={'registry_key': 'fixture-registry', 'clip_uid': 'fixture-clip',
                                  'task_id': 'fixture-task', 'history_name': 'fixture-history'},
                device_meta={}, platform_meta={}, video_meta={}, network_meta={},
                max_retries=1, fail_on_error=True)

    def test_each_native_evaluation_check_requires_id_label_and_nullable_detail(self):
        for check in self.malformed_checks():
            with self.subTest(check=check):
                session = Mock(spec=['request'])
                session.request.return_value = (200, json.dumps({'upload_id': 'fixture-upload', 'checks': [check]}))
                with self.assertRaises(upload.UploadError) as caught:
                    upload.evaluate_upload(session, 'fixture-upload')
                self.assertEqual(caught.exception.phase, 'evaluate')
                session.request.assert_called_once()

    def test_sparse_evaluation_cannot_finalize_or_mark_remote_failed(self):
        for check in self.malformed_checks():
            with self.subTest(check=check):
                session = NativeSession(evaluation={'upload_id': 'fixture-upload', 'checks': [check]})
                result = self.run_session(session)
                self.assertIs(result.finalized, False)
                self.assertEqual(result.chunks[0].upload_id, 'fixture-upload')
                self.assertNotIn('finalize', session.events)
                self.assertNotIn('fail', session.events)
                self.assertFalse(any(method == 'DELETE' for method, _, _ in session.calls))

    def test_quality_schema_keeps_null_text_detail_and_skip_contract(self):
        for detail in (None, '', 'fixture explanation'):
            for status in ('pass', 'fail', 'skip'):
                with self.subTest(detail=detail, status=status):
                    body = {'upload_id': 'fixture-upload', 'checks': [{**self.good_check, 'status': status, 'detail': detail}]}
                    session = Mock(spec=['request'])
                    session.request.return_value = (200, json.dumps(body))
                    self.assertEqual(upload.evaluate_upload(session, 'fixture-upload'), body)
                    self.assertIs(upload.is_perfect(body), status != 'fail')

    def invalid_upload_out(self):
        return [
            {'id': 'fixture-upload'},
            {'id': 'fixture-upload', 'status': 'initiated'},
            {'id': 'fixture-upload', 'meta': {}},
            *({'id': 'fixture-upload', 'status': value, 'meta': {}} for value in (None, True, 1, [], {})),
            *({'id': 'fixture-upload', 'status': 'initiated', 'meta': value} for value in (None, True, 1, [], 'fixture')),
            *({'id': 'fixture-upload', 'status': 'initiated', 'meta': {'blob_path': value}} for value in (None, True, 1, [], {})),
        ]

    def test_native_create_invalid_shape_preserves_known_id_before_sas(self):
        for body in self.invalid_upload_out():
            with self.subTest(body=body):
                session = NativeSession(create=body)
                result = self.fixture.run_chunk(session, fail_on_error=True)
                self.assertEqual(result.state, upload.STATE_QUARANTINE)
                self.assertEqual(result.upload_id, 'fixture-upload')
                self.assertEqual(self.fixture.checkpoint.call_args.kwargs['phase'], 'registration_review')
                self.assertEqual(session.events, ['create'])
                self.fixture.video_put.assert_not_called()
                self.assertEqual(sum(path.startswith('/api/v1/uploads?') for _, path, _ in session.calls), 1)

    def test_native_complete_invalid_shape_preserves_known_id_for_review(self):
        for body in self.invalid_upload_out():
            with self.subTest(body=body):
                session = NativeSession(complete=body)
                result = self.fixture.run_chunk(session, fail_on_error=True)
                self.assertEqual(result.state, upload.STATE_QUARANTINE)
                self.assertEqual(result.upload_id, 'fixture-upload')
                self.assertEqual(self.fixture.checkpoint.call_args.kwargs['phase'], 'completion_review')
                self.assertEqual(session.events, ['create', 'sas', 'put-video', 'complete'])
                self.assertNotIn('fail', session.events)

    def test_native_upload_meta_passes_through_additional_finite_fields(self):
        meta = {'blob_path': 'fixture/container/object', 'future': {'items': [1, 2, 3]}}
        session = NativeSession(create={'id': 'fixture-upload', 'status': 'some-native-string', 'meta': copy.deepcopy(meta)},
            complete={'id': 'fixture-upload', 'status': 'uploaded', 'meta': copy.deepcopy(meta)})
        result = self.fixture.run_chunk(session)
        self.assertEqual(result.state, upload.STATE_DONE)
        self.assertEqual(result.raw_create['meta'], meta)
        self.assertEqual(result.raw_complete['meta'], meta)

    def test_complete_helper_keeps_archived_empty_acknowledgements(self):
        for status, body in ((200, ''), (200, '{}'), (204, '')):
            with self.subTest(status=status, body=body):
                session = Mock(spec=['request'])
                session.request.return_value = (status, body)
                self.assertEqual(upload.complete_upload(session, 'fixture-upload', 21), {})

    def test_bad_native_registration_is_durable_and_cannot_resume(self):
        session = NativeSession(create={'id': 'fixture-upload'})
        session.email = 'fixture@example.invalid'
        journals = self.fixture.video.parent / 'journals'
        with patch.object(upload, 'sidecars_dir', return_value=journals):
            result = self.run_session(session, persist=True)
            self.assertIs(result.finalized, False)
            row = upload.load_sidecar('fixture-session', 0)
            self.assertEqual(row['state'], upload.STATE_QUARANTINE)
            self.assertEqual(row['phase'], 'registration_review')
            self.assertEqual(row['upload_id'], 'fixture-upload')
            self.assertIs(row['native_response_schema'], True)
            path = upload._sidecar_path('fixture-session', 0)
            before = path.read_bytes()
            self.assertEqual(upload.pump_pending(session), [])
            self.assertEqual(path.read_bytes(), before)
            described = recovery._describe([row], {}, lambda *_args: False, {})
            self.assertEqual(described['status'], 'needs_review')
            self.assertIs(described['can_resume'], False)
            self.assertEqual(session.events, ['create'])

    def test_native_schema_marker_survives_complete_recovery(self):
        for native in (False, True):
            with self.subTest(native=native):
                row = {'session_id': 'fixture-session', 'chunk_index': 0, 'log_id': 'fixture-session_0',
                       'org_key': 'fixture-org', 'account_email': 'fixture@example.invalid',
                       'state': upload.STATE_COMPLETING, 'phase': 'transport_done',
                       'upload_id': 'fixture-upload', 'size_bytes': 21, 'duration_ms': 60000,
                       'recorded_at': '2026-10-04T06:55:00.000Z', 'expected_chunk_count': 1,
                       'finalize_requested': True, 'finalized': False, 'evaluation_required': False,
                       'suppress_per_chunk_catbear': True}
                if native:
                    row['native_response_schema'] = True
                session = NativeSession(complete={})
                session.email = 'fixture@example.invalid'
                with patch.object(upload, 'list_sidecars', side_effect=lambda: [row]), \
                        patch.object(upload, 'save_sidecar'), \
                        patch.object(upload, 'load_sidecar', side_effect=lambda *_args: dict(row)), \
                        patch.object(upload, '_remove_sidecar_archive') as remove_archive:
                    result = upload.pump_pending(session, max_retries=1)
                    self.assertIs(result[0]['finalized'], not native)
                    if native:
                        self.assertEqual(result[0]['phase'], 'completion_review')
                        self.assertEqual(result[0]['state'], upload.STATE_QUARANTINE)
                        self.assertEqual(session.events, ['complete'])
                        remove_archive.assert_not_called()
                    else:
                        self.assertNotIn('native_response_schema', result[0])
                        self.assertEqual(session.events, ['complete', 'finalize'])
                        remove_archive.assert_called_once()

    def test_full_native_quality_control_can_finalize(self):
        session = NativeSession(evaluation={'upload_id': 'fixture-upload', 'checks': [self.good_check]})
        result = self.run_session(session)
        self.assertIs(result.finalized, True)
        self.assertEqual(session.events, ['create', 'sas', 'put-video', 'complete', 'evaluate', 'finalize'])
