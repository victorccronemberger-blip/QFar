"""Verified ownership, real retry journals, and confined media eviction."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from moneymin import config, upload
from moneymin import media_lifecycle as lifecycle
from moneymin import nymeria_library as library


class ManagedMediaLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='qmoney-owned-media-'))
        self.root = Path(folder).resolve()
        self.stack.enter_context(patch.multiple(config, DATA_DIR=self.root / 'state',
                                              MEDIA_DATA_DIR=self.root / 'media', SECRETS_DIR=self.root / 'secrets'))
        self.media = self.root / 'media' / 'nymeria'
        self.media.mkdir(parents=True)

    def owned(self, name, payload=b'official verified source', algorithm='sha256'):
        path = self.media / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        lifecycle.record_managed_media(path, root=self.media, provider='nymeria', role='source_download',
                                       expected_digest=hashlib.new(algorithm, payload).hexdigest(), algorithm=algorithm)
        return path

    def test_verified_sha1_source_and_marker_removed_but_manual_source_retained(self):
        owned = self.owned('owned.vrs', algorithm='sha1')
        manual = self.media / 'manual.vrs'
        manual.write_bytes(b'manually supplied recording')
        result = lifecycle.cleanup_managed_media([owned, manual], allowed_roots=(self.media,))
        self.assertEqual(result['files'], 2)
        self.assertFalse(owned.exists())
        self.assertFalse(owned.with_name(owned.name + '.managed.json').exists())
        self.assertEqual(manual.read_bytes(), b'manually supplied recording')

    def test_changed_owned_bytes_invalid_marker_and_outside_path_are_preserved(self):
        owned = self.owned('changed.vrs')
        owned.write_bytes(b'foreign replacement')
        invalid = self.owned('invalid.vrs')
        invalid.with_name(invalid.name + '.managed.json').write_text('{"version":1,"version":1}', 'utf8')
        outside = self.root / 'outside.vrs'
        outside.write_bytes(b'foreign external source')
        result = lifecycle.cleanup_managed_media([owned, invalid, outside], allowed_roots=(self.media,))
        self.assertEqual(result['files'], 0)
        self.assertTrue(all(path.exists() for path in (owned, invalid, outside)))

    def test_wrong_expected_official_hash_never_adopts_existing_file(self):
        path = self.media / 'manual.vrs'
        path.write_bytes(b'manual source')
        with self.assertRaises(ValueError):
            lifecycle.record_managed_media(path, root=self.media, provider='nymeria', role='source_download',
                                           expected_digest='0' * 40, algorithm='sha1')
        self.assertFalse(path.with_name(path.name + '.managed.json').exists())

    def test_provider_records_its_nested_root_and_parent_cleanup_stays_confined(self):
        path = self.media / 'recording/data.vrs'
        path.parent.mkdir()
        path.write_bytes(b'verified provider-owned source')
        lifecycle.record_managed_media(path, root=path.parent, provider='nymeria', role='source_download',
                expected_digest=hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(lifecycle.managed_media_paths([path], allowed_roots=(self.media,)), [path])
        result = lifecycle.cleanup_managed_media([path], allowed_roots=(self.media,))
        self.assertEqual(result['files'], 2)
        self.assertFalse(path.exists())

    def test_changed_during_ownership_publication_is_never_adopted(self):
        path = self.media / 'source.vrs'
        payload = b'verified source before replacement'
        path.write_bytes(payload)
        original = lifecycle._managed_fingerprint
        def replace_after_hash(*args, **kwargs):
            result = original(*args, **kwargs)
            path.write_bytes(b'foreign replacement after verified hash')
            return result
        with patch.object(lifecycle, '_managed_fingerprint', side_effect=replace_after_hash):
            with self.assertRaises(ValueError):
                lifecycle.record_managed_media(path, root=self.media, provider='nymeria', role='source_download',
                    expected_digest=hashlib.sha256(payload).hexdigest())
        self.assertFalse(path.with_name(path.name + '.managed.json').exists())

    def test_large_source_hashing_does_not_hold_journal_publication_barrier(self):
        path = self.media / 'source.vrs'
        payload = b'verified original fixture'
        path.write_bytes(payload)
        original = lifecycle._managed_fingerprint
        admitted = []
        def publication():
            try:
                with lifecycle.media_state_lease():
                    admitted.append(True)
            except ValueError:
                admitted.append(False)
        def fingerprint_with_concurrent_publisher(*args, **kwargs):
            worker = threading.Thread(target=publication)
            worker.start()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            return original(*args, **kwargs)
        with patch.object(lifecycle, '_managed_fingerprint', side_effect=fingerprint_with_concurrent_publisher):
            lifecycle.record_managed_media(path, root=self.media, provider='nymeria', role='source_download',
                expected_digest=hashlib.sha256(payload).hexdigest())
        self.assertEqual(admitted, [True])

    def test_two_owned_encodes_and_real_journal_publication_can_finish_concurrently(self):
        paths = [self.media / (name + '.mp4') for name in ('account_one', 'account_two')]
        for index, path in enumerate(paths):
            path.write_bytes(bytes([index + 1]) * (1024 * 1024))
        ready = threading.Barrier(3)
        errors = []
        original = lifecycle._managed_fingerprint
        def simultaneous_hash(*args, **kwargs):
            ready.wait(3)
            return original(*args, **kwargs)
        def publish(path):
            try:
                lifecycle.record_managed_media(path, root=self.media, provider='nymeria', role='account_video',
                    expected_digest=hashlib.sha256(path.read_bytes()).hexdigest())
            except Exception as exc:
                errors.append(exc)
        with patch.object(lifecycle, '_managed_fingerprint', side_effect=simultaneous_hash):
            workers = [threading.Thread(target=publish, args=(path,)) for path in paths]
            for worker in workers:
                worker.start()
            ready.wait(3)
            upload.save_sidecar(dict(session_id='parallel-owned-journal', account_email='owner@example.test',
                org_key='org-fixture', task_id='task-fixture', chunk_index=0, expected_chunk_count=1,
                state='failed', phase='create', create_attempted=True, video_path=str(paths[0]),
                campaign_context={'registry_key': 'minute|task-fixture', 'clip_uid': 'clip-fixture'}))
            for worker in workers:
                worker.join(5)
                self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(lifecycle.managed_media_paths(paths, allowed_roots=(self.media,)), paths)
        self.assertTrue(upload._sidecar_path('parallel-owned-journal', 0).exists())

    def test_real_pending_journal_protects_owned_media_and_marker(self):
        path = self.owned('pending.vrs')
        upload.save_sidecar(dict(session_id='owned-media-retry', account_email='owner@example.test',
            org_key='org-fixture', task_id='task-fixture', chunk_index=0, expected_chunk_count=1,
            state='failed', phase='create', create_attempted=True, video_path=str(path),
            campaign_context={'registry_key': 'minute|task-fixture', 'clip_uid': 'clip-fixture'}))
        result = lifecycle.cleanup_managed_media([path], allowed_roots=(self.media,))
        self.assertEqual(result['files'], 0)
        self.assertTrue(path.exists())
        self.assertTrue(path.with_name(path.name + '.managed.json').exists())

    def test_actual_corrupt_journal_stops_before_one_owned_file_is_erased(self):
        path = self.owned('owned.vrs')
        bad = upload.sidecars_dir() / 'broken.json'
        bad.write_bytes(b'{"session_id":"x","session_id":"y"}')
        with self.assertRaises(ValueError):
            lifecycle.cleanup_managed_media([path], allowed_roots=(self.media,))
        self.assertTrue(path.exists())
        self.assertEqual(bad.read_bytes(), b'{"session_id":"x","session_id":"y"}')

    def test_hash_reference_and_active_directory_consumer_keep_source(self):
        path = self.owned('sequence/recording_head/data/data.vrs')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        result = lifecycle.cleanup_managed_media([path], allowed_roots=(self.media,),
                    protection={'paths': set(), 'sha256': {digest}})
        self.assertEqual(result['files'], 0)
        result = lifecycle.cleanup_managed_media([path], allowed_roots=(self.media,),
                    protected_paths=(self.media / 'sequence',), protection={'paths': set(), 'sha256': set()})
        self.assertEqual(result['files'], 0)
        self.assertTrue(path.exists())

    def test_symlink_inside_owned_root_never_deletes_its_outside_target(self):
        outside = self.root / 'external'
        outside.mkdir()
        target = outside / 'source.vrs'
        target.write_bytes(b'outside recording')
        link = self.media / 'link'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('Directory symlink unavailable on this Windows runner')
        with self.assertRaises(ValueError):
            lifecycle.record_managed_media(link / 'source.vrs', root=self.media, provider='nymeria',
                    role='source_download', expected_digest=hashlib.sha256(target.read_bytes()).hexdigest())
        result = lifecycle.cleanup_managed_media([link / 'source.vrs'], allowed_roots=(self.media,))
        self.assertEqual(result['files'], 0)
        self.assertEqual(target.read_bytes(), b'outside recording')

    def test_sequence_cleanup_releases_only_owned_sources_and_invalidates_measured_ready(self):
        sid = 'sequence_one'
        paths = [self.owned(sid + '/recording_head/data/' + name) for name in ('data.vrs', 'motion.vrs')]
        paths.append(self.owned('_catalog/archives/' + sid + '/timesync_and_imu.zip'))
        annotation = self.media / sid / 'metadata.json'
        annotation.write_text('{"script":"catalog fixture"}', 'utf8')
        measured = self.media / '_catalog/measured' / (sid + '.json')
        measured.parent.mkdir(parents=True)
        measured.write_text('{"selection_ready":true}', 'utf8')
        with patch.object(library.nymeria, 'clear_caches') as invalidated:
            result = library.cleanup_sequence_sources(sid, self.media)
        self.assertEqual(result['files'], 7)
        self.assertFalse(any(path.exists() for path in paths))
        self.assertFalse(measured.exists())
        self.assertTrue(annotation.exists())
        invalidated.assert_called_once()

    def test_retained_pending_sequence_does_not_invalidate_its_measured_ready(self):
        sid = 'sequence_one'
        path = self.owned(sid + '/recording_head/data/data.vrs')
        measured = self.media / '_catalog/measured' / (sid + '.json')
        measured.parent.mkdir(parents=True)
        measured.write_text('{"selection_ready":true}', 'utf8')
        result = library.cleanup_sequence_sources(sid, self.media, protected_paths=(path,))
        self.assertEqual(result['files'], 0)
        self.assertTrue(path.exists())
        self.assertTrue(measured.exists())

    def test_campaign_acquisition_requests_only_one_recording_with_existing_quota_gate(self):
        sid = 'sequence_one'
        candidate = {'seq_id': sid, 'path': str(self.media / sid)}
        with patch.object(library, 'acquire_sequences') as acquire:
            result = library.ensure_sequence_for_campaign(candidate, min_free_bytes=123)
        self.assertEqual(result, self.media / sid)
        acquire.assert_called_once_with([sid], root=self.media, progress=None, should_stop=None, min_free_bytes=123)
        with patch.object(library, 'acquire_sequences') as acquire:
            with self.assertRaises(library.NymeriaLibraryError):
                library.ensure_sequence_for_campaign(candidate, allow_download=False)
        acquire.assert_not_called()


if __name__ == '__main__':
    unittest.main()
