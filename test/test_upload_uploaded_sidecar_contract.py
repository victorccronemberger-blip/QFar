"""Original ZIP transport for an uploaded receipt; all providers/media are inert.

These fixture bytes isolate driver and journal behavior, not sensor provenance.
"""
import hashlib
import json
import time
import unittest
from unittest.mock import patch

from moneymin import config, upload
import test_upload_response_contract as fixtures


class UploadedSidecarTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.UploadResponseContractTests(
            'test_new_upload_defaults_register_before_sas_and_transport')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.root = fixture.video.parent
        self.journals = self.root / 'journals'
        self.journals.mkdir()
        fixture.stack.enter_context(patch.object(config, 'DATA_DIR', self.root))
        fixture.stack.enter_context(patch.object(config, 'MEDIA_DATA_DIR', self.root))
        fixture.stack.enter_context(patch.object(upload, 'sidecars_dir', return_value=self.journals))
        self.clock = upload.format_recorded_at(time.time() - 300)
        self.payload = b'declared-original-sidecar-driver-fixture'
        self.sid = 'uploaded-original'

    def send(self, *, sas_status=200, zip_status=201, sidecar=True, register_first=True):
        class Session(fixtures.FakeSession):
            email = 'fixture@example.invalid'
            def request(self, method, path, body=None):
                if path.endswith('/evaluate') or path.endswith('/finalize'):
                    self.calls.append((method, path, body))
                    self.events.append('evaluate' if path.endswith('/evaluate') else 'finalize')
                    return (200, json.dumps({'upload_id': 'known-upload', 'checks': [{'id': 'declared-quality-fixture', 'label': 'Declared inert quality', 'status': 'pass', 'detail': None}]})) if path.endswith('/evaluate') else (204, '')
                return super().request(method, path, body)
        session = Session(create_status=409, create_body={'upload_id': 'known-upload', 'status': 'uploaded'},
                          sas_status=sas_status)
        self.fixture.video_put.side_effect = lambda *_a, **_k: session.events.append('put-video') or 201
        self.fixture.zip_put.side_effect = lambda *_a, **_k: session.events.append('put-sidecar') or zip_status
        result = upload.upload_session(session, self.fixture.video, 'fixture-org', task_id='fixture-task',
            session_id=self.sid, recorded_at=self.clock, normalize=False, finalize=True,
            evaluate=True, persist_sidecar=True, sidecar=sidecar,
            sidecar_data=self.payload if sidecar else None, register_first=register_first,
            fail_on_error=False, max_retries=1, retry_backoff=0,
            device_meta={}, platform_meta={}, video_meta={}, network_meta={})
        return session, result

    def assert_zip_only(self, session):
        sas = [body for _m, path, body in session.calls if path == '/api/v1/storage/sas/blobs']
        self.assertEqual(len(sas), 1)
        self.assertEqual(sas[0]['files'], [{'filename': self.sid + '_0.data.zip', 'content_type': 'application/zip'}])
        self.fixture.video_put.assert_not_called()
        self.assertEqual(self.fixture.zip_put.call_args.args[1], self.payload)

    def test_uploaded_transports_original_zip_before_confirmation(self):
        session, result = self.send()
        self.assertEqual(session.events, ['create', 'sas', 'put-sidecar', 'complete', 'evaluate', 'finalize'])
        self.assert_zip_only(session)
        self.assertTrue(result.finalized)
        row = upload.load_sidecar(self.sid, 0)
        self.assertEqual(row['upload_id'], 'known-upload')
        self.assertEqual(row['recorded_at'], self.clock)
        self.assertEqual(row['sidecar_sha256'], hashlib.sha256(self.payload).hexdigest())

    def test_uploaded_without_sidecar_keeps_complete_only_control(self):
        session, result = self.send(sidecar=False)
        self.assertEqual(session.events, ['create', 'complete', 'evaluate', 'finalize'])
        self.fixture.video_put.assert_not_called()
        self.fixture.zip_put.assert_not_called()
        self.assertTrue(result.finalized)

    def test_failed_zip_and_sas_preserve_receipt_and_archive(self):
        for stage in ('sas', 'zip'):
            with self.subTest(stage=stage):
                self.sid = 'failed-' + stage
                session, result = self.send(sas_status=503 if stage == 'sas' else 200,
                                            zip_status=503 if stage == 'zip' else 201)
                self.assertFalse(result.finalized)
                self.assertEqual(result.chunks[0].state, upload.STATE_RETRY_LATE)
                self.assertNotIn('complete', session.events)
                self.assertNotIn('evaluate', session.events)
                self.assertNotIn('finalize', session.events)
                row = upload.load_sidecar(self.sid, 0)
                self.assertEqual(row['upload_id'], 'known-upload')
                self.assertEqual(row['transport_artifact'], 'sidecar')
                self.assertEqual(upload._sidecar_archive_path(self.sid, 0).read_bytes(), self.payload)

    def test_original_zip_resumes_without_video_or_create(self):
        session, result = self.send(zip_status=503)
        self.assertFalse(result.finalized)
        self.fixture.video.unlink()
        session.events.clear()
        session.calls.clear()
        self.fixture.video_put.reset_mock()
        self.fixture.zip_put.side_effect = lambda *_a, **_k: session.events.append('put-sidecar') or 201
        rows = upload.pump_pending(session, max_retries=1, retry_backoff=0, fail_on_error=False)
        self.assertEqual(session.events, ['sas', 'put-sidecar', 'complete', 'evaluate', 'finalize'])
        self.assert_zip_only(session)
        self.assertEqual(rows[0]['upload_id'], 'known-upload')
        self.assertEqual(rows[0]['recorded_at'], self.clock)
        self.assertIs(rows[0]['finalized'], True)

    def test_tampered_missing_or_unbound_archive_rejects_before_effects(self):
        for fault in ('tamper', 'missing', 'hash', 'size', 'time', 'id', 'log', 'path'):
            with self.subTest(fault=fault):
                self.sid = 'invalid-' + fault
                session, result = self.send(zip_status=503)
                self.assertFalse(result.finalized)
                row = upload.load_sidecar(self.sid, 0)
                archive = upload._sidecar_archive_path(self.sid, 0)
                if fault == 'tamper': archive.write_bytes(self.payload + b'changed')
                elif fault == 'missing': archive.unlink()
                elif fault == 'hash': row.pop('sidecar_sha256', None)
                elif fault == 'size': row['size_bytes'] = True
                elif fault == 'time': row['recorded_at'] = ''
                elif fault == 'id': row['upload_id'] = '../other'
                elif fault == 'log': row['log_id'] = 'other_0'
                elif fault == 'path': row['sidecar_data_path'] = str(self.root / 'different.zip')
                journal = upload._sidecar_path(self.sid, 0)
                if fault == 'id':
                    # The public writer now refuses changing a known receipt.
                    # Corruption on disk is still exercised independently of
                    # that earlier guard, rather than weakening the reader.
                    existing = journal.read_bytes()
                    with self.assertRaises(upload.UploadError):
                        upload.save_sidecar(row)
                    self.assertEqual(journal.read_bytes(), existing)
                    journal.write_text(json.dumps(row), encoding='utf8')
                else:
                    upload.save_sidecar(row)
                before = journal.read_bytes()
                session.calls.clear()
                with self.assertRaises(upload.UploadError):
                    upload.pump_pending(session, session_ids={self.sid}, max_retries=1, retry_backoff=0)
                self.assertEqual(session.calls, [])
                self.assertEqual(journal.read_bytes(), before)

    def test_legacy_transport_not_repeated_after_uploaded_conflict(self):
        session, result = self.send(register_first=False)
        self.assertEqual(session.events, ['sas', 'put-video', 'put-sidecar', 'create', 'complete', 'evaluate', 'finalize'])
        self.fixture.video_put.assert_called_once()
        self.fixture.zip_put.assert_called_once()
        self.assertTrue(result.finalized)

    def test_expired_zip_sas_is_renewed_without_mp4_or_create(self):
        for status in (401, 403):
            with self.subTest(status=status):
                self.sid = 'remint-' + str(status)
                session, result = self.send(zip_status=503)
                self.assertFalse(result.finalized)
                session.calls.clear()
                session.events.clear()
                remaining = iter((status, 201))
                payloads = []
                def put_zip(_url, data, **_kwargs):
                    payloads.append(data)
                    session.events.append('put-sidecar')
                    return next(remaining)
                self.fixture.zip_put.side_effect = put_zip
                rows = upload.pump_pending(session, session_ids={self.sid}, max_retries=1,
                                           retry_backoff=0, fail_on_error=False)
                self.assertEqual(session.events, ['sas', 'put-sidecar', 'sas', 'put-sidecar',
                                                  'complete', 'evaluate', 'finalize'])
                self.assertEqual(payloads, [self.payload, self.payload])
                self.fixture.video_put.assert_not_called()
                self.assertIs(rows[0]['finalized'], True)

    def test_resume_sas_requires_zip_and_preserves_original_on_failure(self):
        session, _result = self.send(zip_status=503)
        session.calls.clear()
        session.events.clear()
        session.sas_body = {'signed_urls': [{'filename': self.sid + '_0.mp4',
                                            'blob_url': 'https://blob.invalid/video', 'expires_at': '2030-01-01T00:00:00Z'}]}
        self.fixture.zip_put.reset_mock()
        rows = upload.pump_pending(session, max_retries=1, retry_backoff=0, fail_on_error=False)
        self.assertEqual(session.events, ['sas'])
        self.fixture.zip_put.assert_not_called()
        self.assertEqual(rows[0]['state'], upload.STATE_FAILED)
        self.assertEqual(rows[0]['upload_id'], 'known-upload')
        self.assertEqual(upload._sidecar_archive_path(self.sid, 0).read_bytes(), self.payload)

    def test_private_resume_rejects_owner_mismatch_before_checkpoints(self):
        session, _result = self.send(zip_status=503)
        row = upload.load_sidecar(self.sid, 0)
        session.email = 'different@example.invalid'
        session.calls.clear()
        journal = upload._sidecar_path(self.sid, 0)
        original = journal.read_bytes()
        checkpoints = []
        with self.assertRaises(upload.UploadError):
            upload._upload_single_chunk(session, self.fixture.video, 'fixture-org', self.sid,
                0, 'fixture-task', 'video/mp4', 1, self.clock, None, None, None, None,
                _resume_sidecar_row=row, checkpoint=lambda **kw: checkpoints.append(kw))
        self.assertEqual(session.calls, [])
        self.assertEqual(checkpoints, [])
        self.assertEqual(journal.read_bytes(), original)

    def test_changed_duration_policy_preserves_diagnostic_and_receipt(self):
        session, _result = self.send(zip_status=503)
        session.calls.clear()
        with patch.object(upload.config, 'recording_limits', return_value={
                'min_duration_ms': 90_000, 'max_duration_ms': 1_800_000}):
            rows = upload.pump_pending(session, max_retries=1, retry_backoff=0, fail_on_error=False)
        self.assertEqual(session.calls, [])
        self.assertEqual(rows[0]['state'], upload.STATE_FAILED)
        self.assertEqual(rows[0]['upload_id'], 'known-upload')
        self.assertIn('preflight', rows[0]['error'])
        self.assertEqual(upload._sidecar_archive_path(self.sid, 0).read_bytes(), self.payload)
