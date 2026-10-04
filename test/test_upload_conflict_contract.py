"""Only a canonical, actionable conflict can advance the upload driver.

These are declared offline protocol fixtures, not Android captures or provider
acceptance. Path-safe IDs and unambiguous JSON are local safety requirements.
"""
import json
import time
import unittest
from unittest.mock import patch

from moneymin import config, recovery, upload
import test_upload_response_contract as response_fixtures


class UploadConflictContractTests(unittest.TestCase):
    def setUp(self):
        fixture = response_fixtures.UploadResponseContractTests(
            'test_new_upload_defaults_register_before_sas_and_transport')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        root = fixture.video.parent
        journals = root / 'journals'
        journals.mkdir()
        fixture.stack.enter_context(patch.object(config, 'DATA_DIR', root))
        fixture.stack.enter_context(patch.object(config, 'MEDIA_DATA_DIR', root))
        fixture.stack.enter_context(patch.object(upload, 'sidecars_dir', return_value=journals))
        self.clock = upload.format_recorded_at(time.time() - 300)
        self.sequence = 0

    def send(self, body=None, *, raw=None, status=409, register_first=True, evaluate=False,
             require_perfect=False):
        class ConflictSession(response_fixtures.FakeSession):
            email = 'contract@example.invalid'
            def request(self, method, path, body=None):
                if method == 'DELETE':
                    self.calls.append((method, path, body))
                    self.events.append('delete-upload' if path.startswith('/api/v1/uploads/')
                                       else 'delete-session')
                    return 204, ''
                if path.endswith('/evaluate'):
                    self.calls.append((method, path, body))
                    self.events.append('evaluate')
                    return 200, json.dumps({'upload_id': path.split('/')[-2], 'checks': []})
                if path.endswith('/finalize'):
                    self.calls.append((method, path, body))
                    self.events.append('finalize')
                    return 204, ''
                return super().request(method, path, body)
        self.sequence += 1
        sid = f'conflict-{self.sequence}'
        session = ConflictSession(create_status=status, create_body=body, raw_create=raw)
        self.fixture.video_put.reset_mock()
        self.fixture.zip_put.reset_mock()
        self.fixture.video_put.side_effect = lambda *_a, **_k: session.events.append('put-video') or 201
        result = upload.upload_session(session, self.fixture.video, 'fixture-org',
            task_id='fixture-task', session_id=sid, recorded_at=self.clock,
            normalize=False, finalize=True, evaluate=evaluate, persist_sidecar=True,
            require_perfect=require_perfect,
            register_first=register_first, fail_on_error=True, max_retries=3,
            retry_backoff=0, device_meta={}, platform_meta={}, video_meta={}, network_meta={})
        return session, result, sid

    def assert_dead_end(self, session, result, sid, *, legacy=False):
        chunk = result.chunks[0]
        self.assertEqual(chunk.state, upload.STATE_FAILED)
        self.assertFalse(result.finalized)
        self.assertEqual(session.events, ['sas', 'put-video', 'create'] if legacy else ['create'])
        if legacy:
            # The explicit legacy order has already PUT the default ZIP too.
            self.fixture.zip_put.assert_called_once()
        else:
            self.fixture.zip_put.assert_not_called()
            self.fixture.video_put.assert_not_called()
        row = upload.load_sidecar(sid, 0)
        self.assertEqual(row['session_id'], sid)
        self.assertEqual(row['log_id'], sid + '_0')
        self.assertEqual(row['task_id'], 'fixture-task')
        self.assertEqual(row['recorded_at'], self.clock)
        self.assertEqual(row['account_email'], session.email)
        self.assertIs(row['create_attempted'], True)
        self.assertEqual(row['attempts'], 1)
        self.assertNotIn('private-canary', row.get('error', ''))
        item = next(x for x in recovery.snapshot()['items'] if x['session_id'] == sid)
        self.assertEqual(item['status'], 'needs_review')
        self.assertIs(item['can_resume'], False)
        path = upload._sidecar_path(sid, 0)
        before = path.read_bytes()
        calls = list(session.calls)
        try:
            resumed = upload.pump_pending(session, account_email=session.email, session_ids={sid})
        except upload.UploadError:
            resumed = []
        self.assertEqual(resumed, [])
        self.assertEqual(session.calls, calls)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.fixture.video.read_bytes(), b'fixture-video-content')

    def test_missing_alias_and_nested_conflicts_cannot_authorize_transport(self):
        cases = [{}, {'upload_id': 'existing'}, {'status': 'initiated'},
                 {'id': 'existing', 'status': 'initiated'},
                 {'uploadId': 'existing', 'status': 'uploaded'},
                 {'detail': {'upload_id': 'existing', 'status': 'uploaded'}},
                 {'detail': {'id': 'existing', 'status': 'completed'}},
                 {'upload_id': 'existing', 'status': 'initiated', 'detail': {}},
                 {'upload_id': 'existing', 'status': 'initiated', 'detail': None}]
        for body in cases:
            with self.subTest(body=body):
                self.assert_dead_end(*self.send(body))

    def test_unsupported_states_are_preserved_without_completion_or_finalization(self):
        for state in ('completed', 'complete', 'done', 'created', 'failed', 'unknown',
                      'UPLOADED', ' uploaded ', None, True, 1, [], {}):
            with self.subTest(state=state):
                body = {'upload_id': 'existing', 'status': state, 'detail': 'private-canary'}
                self.assert_dead_end(*self.send(body))

    def test_ambiguous_json_and_unsafe_ids_are_rejected_privately(self):
        raw_cases = [
            '{"upload_id":"a","upload_id":"b","status":"initiated"}',
            '{"upload_id":"a","status":"completed","status":"initiated"}',
            '{"upload_id":"a","status":"initiated","extra":NaN}',
            '{"upload_id":"a","status":"initiated","extra":Infinity}',
            '{"upload_id":"a","status":"initiated","extra":1e999}',
            'private-canary', 'null', '[]', 'true',
        ]
        for raw in raw_cases:
            with self.subTest(raw=raw):
                self.assert_dead_end(*self.send(raw=raw))
        for identity in ('', ' ', '..', 'a/b', 'a?b', 'a%b', '\x00', True, 1, [], {}):
            with self.subTest(identity=identity):
                self.assert_dead_end(*self.send({'upload_id': identity, 'status': 'initiated'}))

    def test_status_alone_controls_valid_conflicts_despite_unrelated_words(self):
        for state in ('initiated', 'uploaded'):
            for detail in ('completed', 'uploaded', 'Session has been deleted'):
                with self.subTest(state=state, detail=detail):
                    session, result, _sid = self.send({'upload_id': 'existing', 'status': state,
                        'detail': detail, 'note': 'done', '_conflict_action': 'done'})
                    self.assertTrue(result.finalized)
                    self.assertEqual(result.chunks[0].upload_id, 'existing')
                    expected = (['create', 'sas', 'put-video', 'complete', 'finalize']
                                if state == 'initiated' else ['create', 'sas', 'complete', 'finalize'])
                    self.assertEqual(session.events, expected)
                    self.fixture.zip_put.assert_called_once()
                    if state == 'uploaded':
                        self.fixture.video_put.assert_not_called()
                    complete = next(path for _method, path, _body in session.calls
                                    if path.endswith('/complete'))
                    self.assertEqual(complete, '/api/v1/uploads/existing/complete')

    def test_success_payload_cannot_supply_an_internal_conflict_decision(self):
        for action in ('done', 'complete', 'reuse-and-upload'):
            with self.subTest(action=action):
                session, result, _sid = self.send(
                    {'id': 'ordinary', 'status': 'initiated', 'meta': {}, '_conflict_action': action}, status=201)
                self.assertTrue(result.finalized)
                self.assertEqual(session.events,
                                 ['create', 'sas', 'put-video', 'complete', 'finalize'])

    def test_legacy_order_stops_at_unsupported_conflict_after_the_existing_put(self):
        session, result, sid = self.send({'upload_id': 'existing', 'status': 'completed'},
                                         register_first=False)
        self.assert_dead_end(session, result, sid, legacy=True)

    def test_failed_conflict_keeps_its_known_identity_without_evaluation(self):
        for perfect in (False, True):
            with self.subTest(require_perfect=perfect):
                session, result, sid = self.send({'upload_id': 'existing', 'status': 'failed'},
                                                 evaluate=True, require_perfect=perfect)
                self.assert_dead_end(session, result, sid)
                self.assertEqual(result.chunks[0].upload_id, 'existing')
                self.assertEqual(upload.load_sidecar(sid, 0)['upload_id'], 'existing')
