"""Real local journals and lifecycle leases; all media are inert temp fixtures."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from moneymin import campaign, config, content_provenance, ego_accelerator, media_lifecycle, upload
from moneymin.media_lifecycle import media_state_lease


HEADER = 'canonical_timestamp_ms,gyro_x,gyro_y,gyro_z,accl_x,accl_y,accl_z\n'


class LibraryProtectionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(
            prefix='ego-library-protection-'))).resolve()
        self.stack.enter_context(patch.multiple(config,
            DATA_DIR=self.root / 'state', MEDIA_DATA_DIR=self.root / 'media',
            SECRETS_DIR=self.root / 'secrets', VIDEOS_DIR=self.root / 'recordings'))
        self.stack.enter_context(patch('socket.socket.connect', side_effect=AssertionError('Network forbidden')))
        self.row = {
            'clip_uid': 'clip-fixture', 'exported_clip_uid': 'clip-fixture',
            'parent_video_uid': 'parent-fixture', 'parent_start_sec': '0',
            'parent_end_sec': '120', 'dur_s': 120, 'needs_cut': False,
            'source': 'ego4d', 's3_path': 's3://inert-fixture/clip-fixture.mp4',
        }
        self.video = {'video_uid': 'parent-fixture', 'has_imu': True}
        self.stack.enter_context(patch.object(ego_accelerator, 'catalog_installed', return_value=True))
        self.stack.enter_context(patch.object(campaign, '_ego_clip_inputs', return_value=(self.row, self.video)))
        self.stack.enter_context(patch.object(ego_accelerator, 'eligible_clips', return_value=[self.row]))
        self.stack.enter_context(patch.object(ego_accelerator, 'allocation_plan', return_value=[self.row]))
        self.work = ego_accelerator.data_dir()
        self.work.mkdir(parents=True)
        plan = campaign._ego_prepare_plan(self.row)
        self.source = self.work / plan['source_name']
        self.native = self.work / plan['native_name']
        self.marker = self.native.with_name(self.native.name + '.source.json')
        self.imu = self.work / plan['imu_name']
        self.variant = self.work / f'{self.native.stem}_accfixture.mp4'
        self.source.write_bytes(b's' * (1024 * 1024 + 1))
        self.native.write_bytes(b'n' * (1024 * 1024 + 1))
        self.marker.write_text('{"version": 1}', encoding='utf-8')
        self.imu.write_text(HEADER + ''.join(f'{i},1,2,3,4,5,6\n' for i in range(0, 120_001, 10)), encoding='utf-8')
        self.variant.write_bytes(b'account variant fixture')
        self.assertTrue(upload.sidecars_dir().resolve().is_relative_to(self.root))

    def pending(self, path=None, *, protected_asset=None):
        context = {'registry_key': 'minute|fixture-task', 'clip_uid': 'clip-fixture'}
        if protected_asset is not None:
            binding = {'content': {'assets': {'fixture_asset': {
                'sha256': hashlib.sha256(protected_asset.read_bytes()).hexdigest(),
            }}}}
            binding['delivery_binding_sha256'] = content_provenance.canonical_digest(binding)
            context['content_provenance'] = binding
        return upload.save_sidecar(dict(
            session_id='pending-fixture', chunk_index=0, expected_chunk_count=1,
            account_email='owner@example.invalid', org_key='fixture-org',
            task_id='fixture-task', state='creating', phase='queued',
            finalized=False, video_path=str((path or self.native).resolve()),
            campaign_context=context,
        ))

    def snapshot(self):
        paths = [*self.work.iterdir(), *upload.sidecars_dir().glob('*.json')]
        return {path: path.read_bytes() for path in paths if path.is_file()}

    def assert_preserved(self, before):
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def warm(self, **kwargs):
        return ego_accelerator.warm_cache(work_dir=self.work, min_free_gb=0, **kwargs)

    def test_reclaim_preserves_pending_native_old_marker_and_actual_journal(self):
        self.pending()
        before = self.snapshot()
        self.assertEqual(ego_accelerator.reclaim_stale_native(self.work), {'files': 0, 'bytes': 0})
        self.assert_preserved(before)

    def test_reclaim_preserves_native_bound_by_hash_at_another_path(self):
        alias = self.root / 'journal-native.mp4'
        alias.write_bytes(self.native.read_bytes())
        self.pending(alias, protected_asset=alias)
        before = self.snapshot()
        self.assertEqual(ego_accelerator.reclaim_stale_native(self.work)['files'], 0)
        self.assert_preserved(before)

    def test_reclaim_removes_only_unprotected_orphan_derivative_and_marker(self):
        keep = {path: path.read_bytes() for path in (self.source, self.imu, self.variant)}
        size = self.native.stat().st_size
        self.assertEqual(ego_accelerator.reclaim_stale_native(self.work), {'files': 1, 'bytes': size})
        self.assertFalse(self.native.exists())
        self.assertFalse(self.marker.exists())
        self.assert_preserved(keep)

    def test_corrupt_journal_blocks_reclaim_before_deletion(self):
        bad = upload.sidecars_dir() / 'corrupt.json'
        bad.write_bytes(b'{"session_id":"one","session_id":"two"}')
        before = self.snapshot()
        with self.assertRaises(ValueError):
            ego_accelerator.reclaim_stale_native(self.work)
        self.assert_preserved(before)

    def test_reclaim_obeys_actual_lifecycle_exclusion(self):
        held, release = threading.Event(), threading.Event()

        def hold():
            with media_state_lease():
                held.set()
                release.wait(5)

        worker = threading.Thread(target=hold)
        worker.start()
        try:
            self.assertTrue(held.wait(2))
            before = self.snapshot()
            with self.assertRaises(ValueError):
                ego_accelerator.reclaim_stale_native(self.work)
            self.assert_preserved(before)
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_warm_preserves_pending_stale_media_without_false_ready(self):
        self.pending()
        before = self.snapshot()
        events = []
        with patch.object(campaign, 'prepare_clip', side_effect=AssertionError('Protected preparation')) as prepare:
            result = self.warm(budget_gb=1, progress=lambda kind, payload: events.append((kind, payload)))
        prepare.assert_not_called()
        self.assertEqual((result['ready'], result['skipped'], result['protected'], result['failed']), (0, 1, 1, 0))
        self.assertEqual(result['reclaimed_files'], 0)
        self.assertEqual([kind for kind, _ in events], ['start', 'protected'])
        self.assert_preserved(before)

    def test_warm_preserves_shared_sensor_protected_by_content_hash(self):
        unrelated = self.root / 'other-delivery.mp4'
        unrelated.write_bytes(b'inert unrelated native')
        self.pending(unrelated, protected_asset=self.imu)
        before = self.snapshot()
        with patch.object(campaign, 'prepare_clip') as prepare:
            result = self.warm()
        prepare.assert_not_called()
        self.assertEqual((result['ready'], result['protected']), (0, 1))
        self.assert_preserved(before)

    def test_journal_published_after_cache_check_is_read_before_prepare(self):
        events = []
        before = {path: path.read_bytes() for path in self.work.iterdir()}

        def progress(kind, payload):
            events.append(kind)
            if kind == 'start':
                self.pending()

        with patch.object(campaign, 'prepare_clip') as prepare:
            result = self.warm(progress=progress)
        prepare.assert_not_called()
        self.assertEqual((result['ready'], result['protected']), (0, 1))
        self.assertEqual(events, ['start', 'protected'])
        self.assert_preserved(before)
        self.assertIsNotNone(upload.load_sidecar('pending-fixture'))

    def test_unreadable_protection_blocks_warm_preparation(self):
        (upload.sidecars_dir() / 'corrupt.json').write_bytes(b'[]')
        before = self.snapshot()
        with patch.object(campaign, 'prepare_clip') as prepare:
            result = self.warm()
        prepare.assert_not_called()
        self.assertEqual((result['ready'], result['failed']), (0, 1))
        self.assert_preserved(before)

    def test_unprotected_orphan_can_be_prepared_under_publication_exclusion(self):
        keep = {path: path.read_bytes() for path in (self.source, self.imu, self.variant)}
        attempted = []

        def prepare(row, video, work, **kwargs):
            self.assertIs(row, self.row)
            self.assertIs(video, self.video)
            self.assertEqual(work, self.work)

            def publish():
                try:
                    self.pending()
                except upload.UploadError as exc:
                    attempted.append(exc)

            worker = threading.Thread(target=publish)
            real_clock = media_lifecycle.time.monotonic
            publisher_clock = iter((0.0, 30.1))

            def clock():
                # Preparation owns the real media barrier. Expire only this
                # competing publisher's bounded wait, without releasing the
                # barrier or delaying the fixture for 30 seconds.
                return next(publisher_clock) if threading.current_thread() is worker else real_clock()

            with patch.object(media_lifecycle.time, 'monotonic', side_effect=clock):
                worker.start()
                worker.join(4)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(attempted), 1)
            self.assertFalse(upload._sidecar_path('pending-fixture').exists())
            self.native.write_bytes(b'r' * (1024 * 1024 + 1))
            plan = campaign._ego_prepare_plan(row)
            self.marker.write_text(json.dumps(campaign._native_cache_marker(
                self.source, self.native, plan['norm_start'], plan['dur_s'])), encoding='utf-8')
            return {}

        with patch.object(campaign, 'prepare_clip', side_effect=prepare) as called:
            result = self.warm(budget_gb=1)
        called.assert_called_once()
        self.assertEqual((result['ready'], result['protected'], result['failed']), (1, 0, 0))
        self.assertEqual(result['reclaimed_files'], 1)
        self.assertEqual(campaign.ego_clip_cache_state(self.row, self.work), 'ready')
        self.assert_preserved(keep)

    def test_ready_pending_cache_is_read_without_replacement(self):
        plan = campaign._ego_prepare_plan(self.row)
        self.marker.write_text(json.dumps(campaign._native_cache_marker(
            self.source, self.native, plan['norm_start'], plan['dur_s'])), encoding='utf-8')
        self.pending(protected_asset=self.native)
        before = self.snapshot()
        self.assertEqual(campaign.ego_clip_cache_state(self.row, self.work), 'ready')
        with patch.object(campaign, 'prepare_clip') as prepare:
            result = self.warm(budget_gb=1)
        prepare.assert_not_called()
        self.assertEqual((result['ready'], result['skipped'], result['protected']), (1, 1, 0))
        self.assert_preserved(before)


if __name__ == '__main__':
    unittest.main()
