"""Pause/stop at real upload protocol boundaries, with inert provider and media."""
import json
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from moneymin import device_profile, upload
from moneymin.web.runner import CampaignRunner


class UploadPauseBoundaryTests(unittest.TestCase):
    def execute(self, boundary, *, transient=False, stop=False, creation_review=False):
        runner = CampaignRunner()
        runner.state = 'running'
        triggered, blocked, finished = threading.Event(), threading.Event(), threading.Event()
        wire, results, errors = [], [], []
        counts = {}

        class InertSession:
            email = 'pause-fixture@example.invalid'

            def ensure_auth(self):
                return {'email': self.email}

            def request(self, method, path, body=None):
                phase = ('complete' if path.endswith('/complete') else
                         'finalize' if path.endswith('/finalize') else
                         'evaluate' if path.endswith('/evaluate') else
                         'create' if path.startswith('/api/v1/uploads?') else
                         'sas' if path == '/api/v1/storage/sas/blobs' else
                         'transport' if method == 'PUT' else 'other')
                counts[phase] = counts.get(phase, 0) + 1
                wire.append((phase, runner.pause_requested))
                if phase == boundary and counts[phase] == 1:
                    runner.stop() if stop else runner.pause()
                    triggered.set()
                    if transient:
                        return 503, '{}'
                if path.startswith('/api/v1/uploads?'):
                    return 201, json.dumps({'id': 'fixture-upload', 'status': 'initiated', 'meta': {}})
                if path == '/api/v1/storage/sas/blobs':
                    return 200, json.dumps({'signed_urls': [
                        {'filename': item['filename'], 'blob_url': 'https://blob.invalid/' + item['filename'], 'expires_at': '2030-01-01T00:00:00Z'}
                        for item in body['files']]})
                if phase == 'complete':
                    return 200, json.dumps({'id': path.split('/')[-2], 'status': 'uploaded', 'meta': {}})
                if phase == 'evaluate':
                    return 200, json.dumps({'upload_id': 'fixture-upload', 'checks': [{'id': 'fixture', 'label': 'Declared inert quality', 'status': 'pass', 'detail': None}]})
                if phase == 'finalize':
                    return 204, ''
                if phase == 'transport':
                    return 201, ''
                raise AssertionError('Unexpected inert operation')

        inert = InertSession()

        def put_file(*args, **kwargs):
            return inert.request('PUT', '/fixture-only-put')[0]

        def progress(*args, **kwargs):
            if runner.pause_requested:
                blocked.set()
            runner._checkpoint()

        with tempfile.TemporaryDirectory(prefix='upload-pause-fixture-') as temporary, ExitStack() as stack:
            root = Path(temporary)
            path = root / 'declared-fixture.mp4'
            path.write_bytes(b'Not a real recording: offline pause protocol fixture.')
            recorded = device_profile.format_recorded_at(time.time() - 180)
            stack.enter_context(patch.object(upload, 'sidecars_dir', return_value=root / 'journals'))
            stack.enter_context(patch.object(upload, '_probe_duration_ms', return_value=60_000))
            stack.enter_context(patch.object(upload, '_put_blob_file', side_effect=put_file))
            stack.enter_context(patch.object(upload, 'probe_video', return_value={
                'duration_ms': 60_000, 'fps': 30, 'width': 1440, 'height': 1080,
                'codec': 'h264', 'has_video': True, 'has_audio': True}))
            stack.enter_context(patch.object(upload.time, 'sleep', return_value=None))

            def send():
                try:
                    results.append(upload.upload_session(inert, path, 'fixture-org',
                        session_id='pause-session', task_id='fixture-task', recorded_at=recorded,
                        normalize=False, sidecar=False, persist_sidecar=True, evaluate=True,
                        finalize=True, register_first=True, max_retries=2, on_progress=progress))
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    finished.set()

            worker = threading.Thread(target=send, daemon=True)
            worker.start()
            try:
                self.assertTrue(triggered.wait(2), 'inert provider reached the selected boundary')
                if creation_review:
                    self.assertTrue(finished.wait(2), 'unknown CREATE must stop without a retry')
                    self.assertFalse(blocked.is_set())
                    self.assertTrue(runner.pause_requested)
                    self.assertEqual(wire, [('create', False)])
                    self.assertEqual(errors, [])
                    self.assertEqual(len(results), 1)
                    self.assertFalse(results[0].finalized)
                    self.assertEqual(results[0].chunks[0].upload_id, '')
                    self.assertEqual(results[0].chunks[0].state, upload.STATE_RETRY_LATE)
                    row = upload.load_sidecar('pause-session')
                    self.assertIs(row['create_attempted'], True)
                    self.assertEqual(row['attempts'], 1)
                    with self.assertRaises(upload.UploadError):
                        upload._pending_recovery_stage(row)
                    return
                if stop:
                    self.assertTrue(finished.wait(2), 'stop releases waiters and drains the active session')
                else:
                    self.assertTrue(blocked.wait(0.3), 'next protocol call must pass through pause checkpoint')
                    self.assertFalse(finished.is_set())
                    self.assertFalse(any(paused for phase, paused in wire), 'no new request begins while paused')
                    self.assertEqual(counts.get(boundary), 1)
            finally:
                if runner.pause_requested:
                    runner.resume()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 1)
            self.assertTrue(results[0].finalized)
            row = upload.load_sidecar('pause-session')
            self.assertIs(row['finalized'], True)
            self.assertIs(row['evaluation_verified'], True)

    def test_pause_after_complete_waits_before_evaluate_and_finalize(self):
        self.execute('complete')

    def test_pause_before_complete_retry_waits_for_resume(self):
        self.execute('complete', transient=True)

    def test_pause_before_finalize_retry_waits_for_resume(self):
        self.execute('finalize', transient=True)

    def test_stop_during_complete_releases_and_finishes_active_session(self):
        self.execute('complete', stop=True)

    def test_uncertain_create_stops_without_retry_while_pause_is_requested(self):
        self.execute('create', transient=True, creation_review=True)

    def test_pause_before_sas_retry_waits_for_resume(self):
        self.execute('sas', transient=True)

    def test_pause_after_sas_waits_before_starting_put(self):
        self.execute('sas')

    def test_pause_before_put_retry_waits_for_resume(self):
        self.execute('transport', transient=True)
