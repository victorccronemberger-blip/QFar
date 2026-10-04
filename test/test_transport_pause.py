"""Pause at actual internal file PUT attempts using an inert local client."""
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from moneymin import transport
from moneymin.web.runner import CampaignRunner


class TransportPauseTests(unittest.TestCase):
    def execute(self, boundary=None, *, stop=False):
        runner = CampaignRunner()
        runner.state = 'running'
        reached, blocked, finished = threading.Event(), threading.Event(), threading.Event()
        wire, results, errors, closed = [], [], [], []
        calls = {}

        class InertClient:
            def put(self, url, **kwargs):
                phase = 'blocklist' if 'comp=blocklist' in url else 'block'
                calls[phase] = calls.get(phase, 0) + 1
                wire.append((phase, runner.pause_requested, kwargs['data']))
                if phase == boundary and calls[phase] == 1:
                    runner.stop() if stop else runner.pause()
                    reached.set()
                    return SimpleNamespace(status_code=503, text='inert retry fixture')
                return SimpleNamespace(status_code=201, text='')

            def close(self):
                closed.append(True)

        def progress(sent, total, elapsed):
            if runner.pause_requested:
                blocked.set()
            runner._checkpoint()

        with tempfile.TemporaryDirectory(prefix='transport-pause-fixture-') as temporary:
            path = Path(temporary) / 'declared-fixture.bin'
            content = b'Offline transport bytes, not a recording.'
            path.write_bytes(content)
            fake = SimpleNamespace(Session=lambda **kwargs: InertClient())
            with patch.object(transport, '_kind', 'curl'), \
                 patch.object(transport, '_cffi', fake), \
                 patch.object(transport, '_impersonate', None), \
                 patch.object(transport, 'BLOCK_SIZE', 8), \
                 patch.object(transport.time, 'sleep', return_value=None):

                def send():
                    try:
                        results.append(transport.put_blob_file(
                            'https://blob.invalid/declared-fixture', path, on_progress=progress))
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        finished.set()

                worker = threading.Thread(target=send, daemon=True)
                worker.start()
                try:
                    if boundary:
                        self.assertTrue(reached.wait(2))
                        if stop:
                            self.assertTrue(finished.wait(2))
                        else:
                            self.assertTrue(blocked.wait(0.3), 'internal retry must consult pause before its request')
                            self.assertFalse(finished.is_set())
                            self.assertFalse(any(paused for _, paused, _ in wire))
                            self.assertEqual(calls[boundary], 1)
                    else:
                        self.assertTrue(finished.wait(2))
                finally:
                    if runner.pause_requested:
                        runner.resume()
                    worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(results, [201])
                self.assertEqual(closed, [True])
                self.assertFalse(any(paused for _, paused, _ in wire))
                blocks = [data for phase, _, data in wire if phase == 'block']
                if boundary == 'block':
                    self.assertEqual(blocks[0], blocks[1], 'retry uses the identical original block')
                    blocks.pop(0)
                self.assertEqual(b''.join(blocks), content)

    def test_block_retry_waits_for_resume(self):
        self.execute('block')

    def test_blocklist_retry_waits_for_resume(self):
        self.execute('blocklist')

    def test_stop_drains_the_active_file_transfer(self):
        self.execute('block', stop=True)

    def test_unpaused_file_retains_all_bytes_and_closes_client(self):
        self.execute()
