"""Real local leases and journals, with no provider or account operations."""
from contextlib import ExitStack
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import subprocess
import sys
import os
from unittest.mock import patch
from unittest.mock import Mock
from types import SimpleNamespace
import zipfile

from moneymin import config, media_lifecycle, recovery, upload, validate
import moneymin.operation_lease as operation_lease_module
from moneymin.operation_lease import operation_lease, OperationLeaseError


class UploadCheckpointContentionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='checkpoint-contention-'))
        self.root = Path(folder)
        self.stack.enter_context(patch.multiple(
            config, DATA_DIR=self.root / 'state', MEDIA_DATA_DIR=self.root / 'media',
            SECRETS_DIR=self.root / 'secrets'))

    def journal(self, sid):
        row = {'session_id': sid, 'account_email': sid + '@example.invalid',
               'org_key': 'fixture-org', 'task_id': 'fixture-task',
               'chunk_index': 0, 'expected_chunk_count': 1,
               'state': 'transport', 'phase': 'sas_ready',
               'create_attempted': True, 'upload_id': 'receipt-' + sid}
        return row, upload.save_sidecar(row)

    def holder(self, lease_path=None):
        entered, release = threading.Event(), threading.Event()
        errors = []

        def hold():
            try:
                lease = operation_lease(lease_path) if lease_path else media_lifecycle.media_state_lease()
                with lease:
                    entered.set()
                    if not release.wait(8):
                        raise AssertionError('test holder was not released')
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(entered.wait(3))
        return thread, release, errors

    def finish_holder(self, thread, release, errors):
        release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_two_sessions_publish_after_a_coherent_reader_exceeds_two_seconds(self):
        rows = [self.journal(sid) for sid in ('first-session', 'second-session')]
        entered, release = threading.Event(), threading.Event()
        completed, errors, observed = [], [], []
        real_read = upload._read_sidecar_file

        def slow_first_read(path):
            if not entered.is_set():
                entered.set()
                if not release.wait(8):
                    raise AssertionError('reader was not released')
            return real_read(path)

        def read():
            try:
                observed.extend(upload.list_sidecars())
            except BaseException as error:
                errors.append(error)

        def publish(row):
            try:
                completed.append(upload.save_sidecar({**row, 'phase': 'transport_done'}))
            except BaseException as error:
                errors.append(error)

        with patch.object(upload, '_read_sidecar_file', side_effect=slow_first_read):
            reader = threading.Thread(target=read)
            reader.start()
            self.assertTrue(entered.wait(3))
            writers = [threading.Thread(target=publish, args=(row,)) for row, _ in rows]
            for writer in writers:
                writer.start()
            try:
                time.sleep(2.15)
                self.assertEqual(completed, [])
                self.assertEqual(errors, [])
            finally:
                release.set()
                for thread in [reader, *writers]:
                    thread.join(5)
                    self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(completed), 2)
        self.assertEqual({row['phase'] for row in observed}, {'sas_ready'})
        for original, path in rows:
            stored = json.loads(path.read_text(encoding='utf8'))
            self.assertEqual(stored['phase'], 'transport_done')
            self.assertEqual(stored['session_id'], original['session_id'])
            self.assertEqual(stored['upload_id'], original['upload_id'])

    def test_publisher_deadline_preserves_existing_bytes_and_receipt(self):
        row, path = self.journal('bounded-session')
        before = path.read_bytes()
        with patch.object(media_lifecycle, 'media_state_lease',
                          side_effect=OperationLeaseError(
                              'Uma operação local está em andamento.', busy=True)):
            with self.assertRaises(upload.UploadError) as raised:
                upload.save_sidecar({**row, 'phase': 'transport_done'})
        self.assertEqual(raised.exception.phase, 'recovery')
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(path.read_bytes(), before)
        upload.save_sidecar({**row, 'phase': 'transport_done'})
        self.assertEqual(upload.load_sidecar(row['session_id'])['upload_id'], row['upload_id'])

    def test_specific_journal_ownership_stays_nonblocking_and_is_not_redeemed(self):
        row, path = self.journal('owned-session')
        before = path.read_bytes()
        lock_path = path.with_suffix('.write.lock')
        thread, release, errors = self.holder(lock_path)
        try:
            started = time.monotonic()
            with self.assertRaises(upload.UploadError):
                upload.save_sidecar({**row, 'phase': 'transport_done'})
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertEqual(path.read_bytes(), before)
            self.assertTrue(lock_path.is_file())
        finally:
            self.finish_holder(thread, release, errors)

    def test_checkpoint_resolves_migration_directory_once_before_the_barrier(self):
        row, path = self.journal('path-session')
        real_directory = upload.sidecars_dir
        with patch.object(upload, 'sidecars_dir', wraps=real_directory) as directory:
            self.assertEqual(upload.save_sidecar({**row, 'phase': 'transport_done'}), path)
        self.assertEqual(directory.call_count, 1)

    def test_reader_contention_is_retryable_and_cleanup_remains_nonblocking(self):
        busy = OperationLeaseError('Uma operação local está em andamento.', busy=True)
        with patch.object(media_lifecycle, 'operation_lease', side_effect=busy), \
             patch.object(media_lifecycle.time, 'monotonic', side_effect=[0.0, 30.1, 30.2]):
            with self.assertRaises(upload.UploadError) as raised:
                upload.list_sidecars()
            self.assertTrue(raised.exception.retryable)
            self.assertEqual(raised.exception.phase, 'recovery')
            self.assertIn('ocupados', str(raised.exception))
        started = time.perf_counter()
        with patch.object(media_lifecycle, 'operation_lease', side_effect=busy):
            with self.assertRaises(OperationLeaseError):
                with media_lifecycle.media_state_lease():
                    self.fail('cleanup unexpectedly acquired another caller\'s lease')
        self.assertLess(time.perf_counter() - started, .5)

    def test_local_reader_wait_deadline_is_bounded_and_retryable(self):
        key = os.path.normcase(str((config.DATA_DIR / '.media-lifecycle.lock').resolve()))

        class BusyLocalLock:
            def acquire(self, *, blocking=True, timeout=-1):
                return False

            def release(self):
                raise AssertionError('unacquired local lock was released')

        with patch.dict(operation_lease_module._LOCKS, {key: BusyLocalLock()}), \
             patch.object(media_lifecycle.time, 'monotonic',
                          side_effect=[0.0, 30.1, 30.2]):
            with self.assertRaises(upload.UploadError) as raised:
                upload.list_sidecars()
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.phase, 'recovery')

    def test_local_lease_io_failure_is_not_misreported_as_contention(self):
        with patch.object(media_lifecycle, 'media_state_lease',
                          side_effect=OperationLeaseError(
                              'Não foi possível reservar a operação local.')):
            with self.assertRaises(upload.UploadError) as raised:
                upload.list_sidecars()
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(raised.exception.phase, 'recovery')
        self.assertNotIn('ocupados', str(raised.exception))
        self.assertIn('reservar a leitura', str(raised.exception))

    def test_listing_remains_reentrant_when_caller_already_holds_media_barrier(self):
        row, _path = self.journal('nested-list-session')
        with media_lifecycle.media_state_lease():
            self.assertEqual(upload.list_sidecars(), [row])

    def test_cross_process_media_lock_contention_is_retryable(self):
        path = config.DATA_DIR / '.media-lifecycle.lock'
        code = ('import sys\nfrom moneymin.operation_lease import operation_lease\n'
                'with operation_lease(sys.argv[1]):\n print("owned", flush=True)\n'
                ' sys.stdin.readline()\n')
        child = subprocess.Popen([sys.executable, '-B', '-c', code, str(path)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'owned')
            with self.assertRaises(upload.UploadError) as raised:
                with patch.object(media_lifecycle.time, 'monotonic',
                                  side_effect=[0.0, 30.1, 30.2]):
                    upload.list_sidecars()
            self.assertTrue(raised.exception.retryable)
            self.assertIn('ocupados', str(raised.exception))
        finally:
            child.communicate('\n', timeout=5)
        self.assertEqual(child.returncode, 0)

    def test_permission_failure_opening_lease_is_not_busy(self):
        with patch.object(Path, 'open', side_effect=PermissionError('private permission detail')):
            with self.assertRaises(OperationLeaseError) as raised:
                with operation_lease(self.root / 'inaccessible.lock'):
                    self.fail('lease unexpectedly acquired')
        self.assertFalse(raised.exception.busy)

    def test_large_journal_listing_waits_for_concurrent_checkpoint_barrier(self):
        total = 11_283
        journal_dir = upload.sidecars_dir()
        journal_dir.mkdir(parents=True, exist_ok=True)
        for index in range(total):
            sid = f'bulk-session-{index:05d}'
            path = journal_dir / upload._sidecar_filename(sid, 0)
            path.write_text(json.dumps({'session_id': sid, 'chunk_index': 0,
                                        'state': 'done'}), encoding='utf8')

        entered, release = threading.Event(), threading.Event()
        readers_ready = threading.Barrier(4)
        errors, observed = [], []

        def holder():
            try:
                with media_lifecycle.media_state_lease():
                    entered.set()
                    if not release.wait(10):
                        raise AssertionError('lease holder was not released')
            except BaseException as error:
                errors.append(error)

        def reader():
            try:
                readers_ready.wait(timeout=5)
                observed.append(upload.list_sidecars())
            except BaseException as error:
                errors.append(error)

        holder_thread = threading.Thread(target=holder)
        reader_threads = [threading.Thread(target=reader) for _ in range(3)]
        holder_thread.start()
        self.assertTrue(entered.wait(3))
        try:
            for thread in reader_threads:
                thread.start()
            readers_ready.wait(timeout=5)
            # Give the reader a scheduling turn to enter the contended lease;
            # the bound itself is not tested with a timing threshold here.
            time.sleep(0.05)
        finally:
            release.set()
            for thread in (holder_thread, *reader_threads):
                if thread.ident is not None:
                    thread.join(60)
                    self.assertFalse(thread.is_alive())

        self.assertEqual(errors, [])
        self.assertEqual(len(observed), 3)
        for snapshot in observed:
            self.assertEqual(len(snapshot), total)
            self.assertEqual(snapshot[0]['session_id'], 'bulk-session-00000')
            self.assertEqual(snapshot[-1]['session_id'], f'bulk-session-{total - 1:05d}')

    def test_successful_transport_waits_for_reader_without_repeating_http(self):
        sid = 'transport-session'
        row, path = self.journal(sid)
        row.update(upload_id=None, create_attempted=False, phase='queued')
        # This is a new isolated receipt, not a replacement of the known
        # receipt used by journal(). Keep the public identity guard intact.
        path.unlink()
        upload.save_sidecar(row)
        video = self.root / 'fixture.mp4'
        video.write_bytes(b'declared-fixture-video')
        calls, observed, errors = [], [], []
        entered, release = threading.Event(), threading.Event()
        reader_threads = []
        real_read = upload._read_sidecar_file

        def read_under_barrier(path):
            if threading.current_thread().name == 'checkpoint-fixture-reader':
                entered.set()
                if not release.wait(8):
                    raise AssertionError('fixture reader was not released')
            return real_read(path)

        def read():
            try:
                observed.extend(upload.list_sidecars())
            except BaseException as error:
                errors.append(error)

        def put_zip(*_args, **_kwargs):
            calls.append('put-zip')
            thread = threading.Thread(target=read, name='checkpoint-fixture-reader')
            reader_threads.append(thread)
            thread.start()
            self.assertTrue(entered.wait(3))
            return 201

        def checkpoint(**updates):
            if updates.get('phase') == 'transport_done':
                # Both PUTs have returned successfully. The real coherent
                # reader still owns the barrier past the previous deadline.
                releaser = threading.Thread(target=lambda: (time.sleep(2.15), release.set()))
                reader_threads.append(releaser)
                releaser.start()
            row.update(updates)
            upload.save_sidecar(row)

        def request(method, route, body=None):
            if method == 'POST' and route.startswith('/api/v1/uploads?'):
                calls.append('create')
                return 201, json.dumps({'id': 'receipt-transport-session', 'status': 'initiated', 'meta': {}})
            if method == 'POST' and route == '/api/v1/storage/sas/blobs':
                calls.append('sas')
                return 200, json.dumps({'signed_urls': [
                    {'filename': item['filename'], 'blob_url': 'https://blob.invalid/' + item['filename'],
                     'expires_at': '2030-01-01T00:00:00Z'} for item in body['files']]})
            if method == 'PATCH' and route == '/api/v1/uploads/receipt-transport-session/complete':
                calls.append('complete')
                return 200, json.dumps({'id': 'receipt-transport-session', 'status': 'uploaded', 'meta': {}})
            raise AssertionError('Unexpected fixture request: ' + method + ' ' + route)

        with ExitStack() as stack:
            stack.enter_context(patch.object(upload, '_read_sidecar_file', side_effect=read_under_barrier))
            stack.enter_context(patch.object(upload, '_probe_duration_ms', return_value=60_000))
            stack.enter_context(patch.object(config, 'recording_limits', return_value={
                'min_duration_ms': 60_000, 'max_duration_ms': 1_800_000}))
            stack.enter_context(patch.object(upload, 'probe_video', return_value={}))
            stack.enter_context(patch.object(upload, 'build_metadata_json', return_value={'source': 'fixture'}))
            stack.enter_context(patch.object(upload, '_validate_sidecar_zip', return_value={'source': 'fixture'}))
            stack.enter_context(patch.object(validate, 'validate_upload_meta', return_value=[]))
            video_put = stack.enter_context(patch.object(upload, '_put_blob_file',
                side_effect=lambda *_args, **_kwargs: calls.append('put-video') or 201))
            zip_put = stack.enter_context(patch.object(upload, '_put_blob', side_effect=put_zip))
            try:
                result = upload._upload_single_chunk(
                    session=SimpleNamespace(email=row['account_email'], request=request),
                    video_path=video, org_key=row['org_key'], session_id=sid,
                    chunk_index=0, task_id=row['task_id'], content_type='video/mp4',
                    timeout_blob=1, recorded_at='2026-10-03T00:00:00.000Z',
                    device_meta={}, platform_meta={}, video_meta={}, network_meta={},
                    max_retries=3, retry_backoff=0, checkpoint=checkpoint,
                    sidecar_data=b'declared-fixture-sidecar', fail_on_error=False)
            finally:
                release.set()
                for thread in reader_threads:
                    thread.join(5)
                    self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result.state, upload.STATE_DONE)
        self.assertEqual(calls, ['create', 'sas', 'put-video', 'put-zip', 'complete'])
        self.assertEqual(video_put.call_count, 1)
        self.assertEqual(zip_put.call_count, 1)
        self.assertEqual(observed[0]['phase'], 'sas_ready')
        stored = upload.load_sidecar(sid)
        self.assertEqual(stored['upload_id'], 'receipt-transport-session')
        self.assertEqual(stored['phase'], 'done')

    def test_sas_ready_receipt_without_transport_proof_stays_reserved_before_effects(self):
        sid = 'ambiguous-transport'
        row, path = self.journal(sid)
        video = self.root / 'original.mp4'
        video.write_bytes(b'original-persisted-video')
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, 'w') as archive:
            archive.writestr(sid + '_0.metadata.json', '{"fixture":true}')
            archive.writestr(sid + '_0.imu.csv', 'timestamp,fixture\n0,1\n')
            archive.writestr(sid + '_0.frames.csv', 'timestamp,fixture\n0,1\n')
        archive = upload._sidecar_archive_path(sid)
        archive.write_bytes(payload.getvalue())
        row.update(register_first=True, native_response_schema=True,
                   recorded_at='2026-10-03T00:00:00.000Z', size_bytes=video.stat().st_size,
                   duration_ms=316_000, log_id=sid + '_0', filename=sid + '_0.mp4',
                   local_video_path=str(video), video_content_sha256=hashlib.sha256(video.read_bytes()).hexdigest(),
                   sidecar_data_path=str(archive), sidecar_size_bytes=archive.stat().st_size,
                   sidecar_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                   evaluation_required=True, evaluation_verified=False,
                   finalize_requested=True, finalized=False,
                   campaign_context={'registry_key': 'fixture-task', 'clip_uid': 'reserved-clip'})
        session = SimpleNamespace(email=row['account_email'], request=Mock())
        for local_state in ('bound', 'missing-zip', 'mismatched-video', 'missing-binding'):
            with self.subTest(local_state=local_state):
                archive.write_bytes(payload.getvalue())
                video.write_bytes(b'original-persisted-video')
                candidate = dict(row)
                if local_state == 'missing-zip':
                    archive.unlink()
                elif local_state == 'mismatched-video':
                    video.write_bytes(b'changed-video')
                elif local_state == 'missing-binding':
                    candidate.pop('sidecar_sha256')
                upload.save_sidecar(candidate)
                before = path.read_bytes()
                video_before = video.read_bytes()
                zip_before = archive.read_bytes() if archive.exists() else None
                item = recovery.snapshot()['items'][0]
                self.assertEqual(item['status'], 'needs_review')
                self.assertFalse(item['can_resume'])
                self.assertEqual(recovery.campaign_exclusions([item]),
                                 {'reserved-clip': [row['account_email']]})
                with patch.object(upload, 'save_sidecar', wraps=upload.save_sidecar) as save, \
                        patch.object(upload, '_put_blob_file') as video_put, \
                        patch.object(upload, '_put_blob') as zip_put, \
                        patch.object(upload, 'upload_session') as send:
                    with self.assertRaises(upload.UploadError):
                        upload.pump_pending(session, session_ids={sid})
                save.assert_not_called()
                send.assert_not_called()
                session.request.assert_not_called()
                video_put.assert_not_called()
                zip_put.assert_not_called()
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(video.read_bytes(), video_before)
                self.assertEqual(archive.read_bytes() if archive.exists() else None, zip_before)


if __name__ == '__main__':
    unittest.main()
