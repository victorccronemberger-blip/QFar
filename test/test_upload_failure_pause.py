"""Permanent failure compensation respects pause; inert HTTP and media only."""
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


class UploadFailurePauseTests(unittest.TestCase):
    def execute(self, boundary, *, stop=False):
        runner = CampaignRunner()
        runner.state = 'running'
        reached, blocked, finished = threading.Event(), threading.Event(), threading.Event()
        wire, results, errors = [], [], []

        class InertSession:
            email = 'failure-fixture@example.invalid'

            def request(self, method, path, body=None):
                phase = ('sas' if path == '/api/v1/storage/sas/blobs' else
                         'transport' if method == 'PUT' else
                         'complete' if path.endswith('/complete') else
                         'fail' if path.endswith('/fail') else 'create')
                wire.append((phase, runner.pause_requested))
                if phase == boundary:
                    runner.stop() if stop else runner.pause()
                    reached.set()
                    return 422, '{}'
                if phase == 'create':
                    return 201, json.dumps({'id': 'failure-upload', 'status': 'initiated', 'meta': {}})
                if phase == 'sas':
                    return 200, json.dumps({'signed_urls': [
                        {'filename': item['filename'], 'blob_url': 'https://blob.invalid/' + item['filename'], 'expires_at': '2030-01-01T00:00:00Z'}
                        for item in body['files']]})
                if phase == 'transport':
                    return 201, ''
                if phase == 'complete':
                    return 200, json.dumps({'id': path.split('/')[-2], 'status': 'uploaded', 'meta': {}})
                if phase == 'fail':
                    return 200, '{}'
                raise AssertionError('Unexpected inert operation')

        inert = InertSession()

        def progress(*args, **kwargs):
            if runner.pause_requested:
                blocked.set()
            runner._checkpoint()

        with tempfile.TemporaryDirectory(prefix='failure-pause-fixture-') as temporary, ExitStack() as stack:
            path = Path(temporary) / 'declared-fixture.mp4'
            path.write_bytes(b'Offline failure boundary fixture; not an actual recording.')
            stack.enter_context(patch.object(upload, '_probe_duration_ms', return_value=60_000))
            stack.enter_context(patch.object(upload, '_put_blob_file', side_effect=lambda *a, **k: inert.request('PUT', '/fixture')[0]))
            stack.enter_context(patch.object(upload, 'probe_video', return_value={
                'duration_ms': 60_000, 'fps': 30, 'width': 1440, 'height': 1080,
                'codec': 'h264', 'has_video': True, 'has_audio': True}))

            def send():
                try:
                    results.append(upload._upload_single_chunk(
                        inert, path, 'fixture-org', 'failure-session', 0, 'fixture-task',
                        'video/mp4', 10, device_profile.format_recorded_at(time.time() - 180),
                        None, None, None, None, sidecar=False, max_retries=1, on_progress=progress))
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    finished.set()

            worker = threading.Thread(target=send, daemon=True)
            worker.start()
            try:
                self.assertTrue(reached.wait(2))
                if stop:
                    self.assertTrue(finished.wait(2), 'stop drains the compensation of an active failed upload')
                else:
                    self.assertTrue(blocked.wait(0.3), 'compensation must consult the pause checkpoint')
                    self.assertFalse(finished.is_set())
                    self.assertFalse(any(phase == 'fail' for phase, _ in wire))
            finally:
                if runner.pause_requested:
                    runner.resume()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].state, upload.STATE_FAILED)
            self.assertEqual(results[0].upload_id, 'failure-upload')
            self.assertIn('422', results[0].error)
            self.assertEqual(sum(phase == 'fail' for phase, _ in wire), 1)
            self.assertFalse(any(paused for _, paused in wire))

    def test_pause_after_permanent_sas_failure_waits_before_fail(self):
        self.execute('sas')

    def test_pause_after_permanent_transport_failure_waits_before_fail(self):
        self.execute('transport')

    def test_pause_after_permanent_complete_failure_waits_before_fail(self):
        self.execute('complete')

    def test_stop_after_permanent_complete_failure_drains_compensation(self):
        self.execute('complete', stop=True)
