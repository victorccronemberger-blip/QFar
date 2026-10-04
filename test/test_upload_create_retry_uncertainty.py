"""An accepted-but-unacknowledged CREATE cannot authorize a replacement ID."""
import json
import time
import unittest
from unittest.mock import patch

from moneymin import config, recovery, upload
import test_upload_response_contract as response_fixtures


class UploadCreateRetryUncertaintyTests(unittest.TestCase):
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
        fixture.stack.enter_context(patch.object(upload.time, 'sleep', return_value=None))
        self.clock = upload.format_recorded_at(time.time() - 300)

    def session(self, loss):
        class AcceptedThenLost(response_fixtures.FakeSession):
            email = 'retry@example.invalid'
            def __init__(self):
                super().__init__()
                self.accepted = []
            def request(self, method, path, body=None):
                if path.startswith('/api/v1/uploads?'):
                    self.calls.append((method, path, body))
                    identity = 'accepted-' + str(len(self.accepted) + 1)
                    self.accepted.append(identity)
                    if len(self.accepted) == 1:
                        if loss == 'upload-error':
                            raise upload.UploadError('private-adapter-canary', transient=True,
                                                     status_code=503, phase='create')
                        if loss == 'timeout':
                            raise TimeoutError('private-adapter-canary')
                        if loss == 'malformed':
                            return 201, 'private-adapter-canary'
                        return loss, 'private-adapter-canary'
                    return 201, json.dumps({'id': identity})
                return super().request(method, path, body)
        return AcceptedThenLost()

    def send(self, session, sid, *, register_first=True):
        return upload.upload_session(session, self.fixture.video, 'fixture-org',
            task_id='fixture-task', session_id=sid, recorded_at=self.clock,
            normalize=False, finalize=False, evaluate=False, persist_sidecar=True,
            register_first=register_first, fail_on_error=True,
            device_meta={}, platform_meta={}, video_meta={}, network_meta={},
            max_retries=3, retry_backoff=0)

    def assert_preserved(self, session, result, sid):
        self.assertEqual(session.accepted, ['accepted-1'])
        self.assertEqual(result.chunks[0].upload_id, '')
        self.assertNotEqual(result.chunks[0].state, upload.STATE_DONE)
        self.assertFalse(result.finalized)
        row = upload.load_sidecar(sid, 0)
        self.assertIs(row['create_attempted'], True)
        self.assertEqual(row['attempts'], 1)
        self.assertNotIn('private-adapter-canary', row.get('error', ''))
        path = upload._sidecar_path(sid, 0)
        before = path.read_bytes()
        item = next(item for item in recovery.snapshot()['items'] if item['session_id'] == sid)
        self.assertEqual(item['status'], 'needs_review')
        self.assertIs(item['can_resume'], False)
        try:
            resumed = upload.pump_pending(session, account_email=session.email, session_ids={sid})
        except upload.UploadError:
            resumed = []
        self.assertEqual(resumed, [])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(session.accepted, ['accepted-1'])
        self.assertFalse(any(path.endswith(('/complete', '/fail', '/finalize'))
                             for _method, path, _body in session.calls))

    def test_unknown_http_acknowledgements_do_not_create_replacement_receipts(self):
        for loss in (-1, 408, 429, 500, 503, 504):
            with self.subTest(loss=loss):
                self.fixture.video_put.reset_mock()
                session = self.session(loss)
                sid = 'http-' + str(abs(loss))
                result = self.send(session, sid)
                self.assert_preserved(session, result, sid)
                self.fixture.video_put.assert_not_called()
                self.assertIn(str(loss), result.chunks[0].error)

    def test_adapter_exceptions_keep_a_private_reviewable_record(self):
        for loss in ('upload-error', 'timeout'):
            with self.subTest(loss=loss):
                self.fixture.video_put.reset_mock()
                session = self.session(loss)
                result = self.send(session, loss)
                self.assert_preserved(session, result, loss)
                self.fixture.video_put.assert_not_called()

    def test_malformed_success_is_already_preserved_without_another_create(self):
        session = self.session('malformed')
        result = self.send(session, 'malformed')
        self.assert_preserved(session, result, 'malformed')
        self.fixture.video_put.assert_not_called()

    def test_explicit_legacy_order_does_not_replace_an_uncertain_receipt_after_put(self):
        session = self.session(503)
        result = self.send(session, 'legacy-order', register_first=False)
        self.assert_preserved(session, result, 'legacy-order')
        self.assertEqual(self.fixture.video_put.call_count, 1)

    def test_conflict_with_a_known_receipt_continues_without_new_identity(self):
        session = response_fixtures.FakeSession(
            create_status=409, create_body={'upload_id': 'known-existing', 'status': 'uploaded'})
        session.email = 'retry@example.invalid'
        result = self.send(session, 'known-conflict')
        self.assertEqual(result.chunks[0].upload_id, 'known-existing')
        self.assertEqual(result.chunks[0].state, upload.STATE_DONE)
        self.fixture.video_put.assert_not_called()
        self.assertEqual(sum(path.startswith('/api/v1/uploads?') for _method, path, _body in session.calls), 1)
        self.assertEqual(sum(path.endswith('/complete') for _method, path, _body in session.calls), 1)
