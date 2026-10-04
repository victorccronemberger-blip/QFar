"""Campaign upload caller -> real ZIP transport, declared inert artifacts only."""
import json
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from moneymin import device_profile, transport, upload
from moneymin.web.runner import CampaignRunner


class UploadZipPauseTests(unittest.TestCase):
    def execute(self, control='pause'):
        runner = CampaignRunner()
        runner.state = 'running'
        reached, blocked, finished = threading.Event(), threading.Event(), threading.Event()
        wire, results, errors = [], [], []
        payload = b'Declared opaque ZIP transport fixture; not real sensor data.'

        class InertBlobClient:
            def put(self, url, **kwargs):
                phase = 'blocklist' if 'comp=blocklist' in url else 'block'
                wire.append((phase, runner.pause_requested, kwargs['data']))
                if phase == 'block' and len(wire) == 1:
                    if control == 'pause':
                        runner.pause()
                    elif control == 'stop':
                        runner.stop()
                    reached.set()
                return SimpleNamespace(status_code=201, text='')

        class InertSession:
            def request(self, method, path, body=None):
                if path.startswith('/api/v1/uploads?'):
                    return 201, json.dumps({'id': 'zip-fixture-upload', 'status': 'initiated', 'meta': {}})
                if path == '/api/v1/storage/sas/blobs':
                    return 200, json.dumps({'signed_urls': [
                        {'filename': item['filename'], 'blob_url': 'https://blob.invalid/' + item['filename'], 'expires_at': '2030-01-01T00:00:00Z'}
                        for item in body['files']]})
                if path.endswith('/complete'):
                    return 200, json.dumps({'id': path.split('/')[-2], 'status': 'uploaded', 'meta': {}})
                raise AssertionError('Unexpected inert API operation')

        def progress(*args, **kwargs):
            if runner.pause_requested:
                blocked.set()
            runner._checkpoint()

        with tempfile.TemporaryDirectory(prefix='zip-caller-pause-fixture-') as temporary, ExitStack() as stack:
            path = Path(temporary) / 'declared-fixture.mp4'
            path.write_bytes(b'Declared opaque video fixture; not a recording.')
            stack.enter_context(patch.object(upload, '_probe_duration_ms', return_value=60_000))
            stack.enter_context(patch.object(upload, '_put_blob_file', return_value=201))
            # These declared mocks isolate transport; they do not certify archive/schema/media quality.
            stack.enter_context(patch.object(upload, '_validate_sidecar_zip', return_value={'fixture': True}))
            stack.enter_context(patch('moneymin.validate.validate_upload_meta', return_value=[]))
            stack.enter_context(patch.object(transport, '_kind', 'curl'))
            stack.enter_context(patch.object(transport, '_cffi', InertBlobClient()))
            stack.enter_context(patch.object(transport, '_impersonate', None))
            stack.enter_context(patch.object(transport, 'BLOCK_SIZE', 8))

            def send():
                try:
                    results.append(upload._upload_single_chunk(
                        InertSession(), path, 'fixture-org', 'zip-fixture-session', 0,
                        'fixture-task', 'video/mp4', 10,
                        device_profile.format_recorded_at(time.time() - 180),
                        None, None, None, None, sidecar=True, sidecar_data=payload,
                        max_retries=1, on_progress=progress))
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    finished.set()

            worker = threading.Thread(target=send, daemon=True)
            worker.start()
            try:
                self.assertTrue(reached.wait(2))
                if control == 'pause':
                    self.assertTrue(blocked.wait(0.3))
                    self.assertFalse(finished.is_set())
                    self.assertFalse(any(paused for _, paused, _ in wire), 'ZIP continuation waits before its next internal HTTP request')
                    self.assertEqual(len(wire), 1)
                else:
                    self.assertTrue(finished.wait(2))
            finally:
                if runner.pause_requested:
                    runner.resume()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].state, upload.STATE_DONE)
            self.assertEqual(results[0].upload_id, 'zip-fixture-upload')
            self.assertFalse(any(paused for _, paused, _ in wire))
            self.assertEqual(b''.join(data for phase, _, data in wire if phase == 'block'), payload)
            self.assertEqual(sum(phase == 'blocklist' for phase, _, _ in wire), 1)

    def test_pause_between_zip_blocks_waits_through_actual_upload_caller(self):
        self.execute()

    def test_stop_drains_zip_through_actual_upload_caller(self):
        self.execute('stop')

    def test_unpaused_zip_retains_its_original_bytes(self):
        self.execute('none')
