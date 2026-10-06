"""Local cache observations, using inert bytes and real marker decoders."""
from contextlib import ExitStack
import csv
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from moneymin import (campaign, config, content_provenance, ego4d, prepared_library,
                      token_store, upload, upload_storage)


class PreparedLibraryTests(unittest.TestCase):
    PARENT = '11111111-1111-4111-8111-111111111111'
    CLIP = '22222222-2222-4222-8222-222222222222'

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temporary = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='prepared-library-'))
        self.base = Path(temporary)
        self.root = self.base / 'media' / 'ego4d'
        self.root.mkdir(parents=True)
        self.state = self.base / 'state'
        self.stack.enter_context(patch.multiple(config, DATA_DIR=self.state,
                                               MEDIA_DATA_DIR=self.root.parent,
                                               SECRETS_DIR=self.base / 'secrets'))
        prepared_library._SNAPSHOTS.clear()
        (self.root / 'ego4d.json').write_text(json.dumps({'videos': [
            {'video_uid': self.PARENT, 'duration_sec': 600}]}), encoding='utf8')
        with (self.root / 'clips.csv').open('w', encoding='utf8', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['exported_clip_uid', 'parent_video_uid', 'parent_start_sec', 'parent_end_sec'])
            writer.writerow([self.CLIP, self.PARENT, 30, 210])
        self.sensor = self.root / (self.PARENT + '_imu.csv')
        self.sensor.write_text('canonical_timestamp_ms,gyro_x,gyro_y,gyro_z,accl_x,accl_y,accl_z\n'
                               + '0,0,0,0,0,0,0\n' * 20, encoding='utf8')
        self.source = self.root / (self.PARENT + '.mp4')
        self.source.write_bytes(b'inert original fixture' * 60_000)
        self.uid = self.PARENT + '_30.000_210.000'
        self.native, self.marker = self.make_native(self.uid, self.source, 30.0, 180.0)

    def make_native(self, uid, source, start, duration):
        native = self.root / (uid + '_native.mp4')
        native.write_bytes(b'inert normalized fixture' * 60_000)
        marker = native.with_name(native.name + '.source.json')
        marker.write_text(json.dumps(campaign._native_cache_marker(source, native, start, duration)), encoding='utf8')
        return native, marker

    def listing(self, **kwargs):
        return prepared_library.list_prepared_clips(self.root, **kwargs)

    def journal(self, **changes):
        directory = self.state / 'sidecars'
        directory.mkdir(parents=True, exist_ok=True)
        row = {'session_id': 'fixture-session', 'account_email': 'fixture@example.invalid',
               'org_key': 'fixture-org', 'task_id': 'fixture-task', 'chunk_index': 0,
               'expected_chunk_count': 1, 'state': 'failed', 'phase': 'create',
               'create_attempted': True, 'video_path': str(self.native)}
        row.update(changes)
        path = directory / 'fixture-session.json'
        path.write_text(json.dumps(row), encoding='utf8')
        return path

    def test_ready_is_content_bound_cache_with_no_task_or_zip_claim(self):
        result = self.listing()
        self.assertEqual(result['schema'], 1)
        self.assertEqual(result['library_root'], str(self.root.resolve()))
        self.assertEqual(result['counts']['ready'], 1)
        item = result['items'][0]
        self.assertEqual(item['cache_state'], 'ready')
        self.assertEqual(item['parent_video_uid'], self.PARENT)
        self.assertEqual(item['window_s'], [30, 210])
        self.assertEqual(item['duration_ms'], 180_000)
        self.assertIsNone(item['prepared_duration_ms'])
        self.assertIsNone(item['task_name'])
        self.assertEqual(result['task_approval'], 'not_evaluated')
        self.assertEqual(result['zip_readiness'], 'not_evaluated')
        self.assertEqual(item['marker']['binding_status'], 'verified')

    def test_reads_do_not_install_migrate_encode_or_lookup_accounts(self):
        before = {p: p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        with patch.object(ego4d, '_s3', side_effect=AssertionError('provider')), \
             patch.object(ego4d, 'download_clip', side_effect=AssertionError('download')), \
             patch.object(campaign, 'prepare_clip', side_effect=AssertionError('prepare')), \
             patch.object(campaign, '_ffmpeg_run', side_effect=AssertionError('encode')), \
             patch.object(campaign, 'probe_video', side_effect=AssertionError('probe')), \
             patch.object(token_store, 'records', side_effect=AssertionError('accounts')), \
             patch.object(upload_storage, 'journal_directory', side_effect=AssertionError('migration')):
            self.assertEqual(self.listing()['counts']['ready'], 1)
        self.assertFalse(self.state.exists())
        self.assertEqual(before, {p: p.read_bytes() for p in self.base.rglob('*') if p.is_file()})

    def test_poll_reuses_hash_snapshot_and_shared_sources_are_hashed_once(self):
        self.make_native(self.PARENT + '_210.000_390.000', self.source, 210.0, 180.0)
        real = campaign._native_cache_fingerprint
        with patch.object(campaign, '_native_cache_fingerprint', wraps=real) as fingerprint:
            first = self.listing()
            second = self.listing()
        self.assertEqual(first['generation'], second['generation'])
        self.assertEqual(fingerprint.call_count, 3)  # two natives and one shared source
        self.assertEqual(first['counts']['ready'], 2)
        self.assertLess(first['local_bytes'], sum(item['local_bytes'] for item in first['items']))
        first['items'][0]['cache_state'] = 'forged'
        self.assertEqual(self.listing()['counts']['ready'], 2)

    def test_ttl_boundary_does_not_change_physical_signature_or_reject_scan(self):
        signature = prepared_library.inventory_signature(self.root)
        with patch.object(prepared_library.time, 'monotonic', side_effect=[299.9, 300.1]):
            self.assertEqual(self.listing(force_refresh=True)['counts']['ready'], 1)
        self.assertEqual(signature, prepared_library.inventory_signature(self.root))

    def test_long_scan_freshness_starts_at_completion_and_next_poll_does_not_rehash(self):
        real = campaign._native_cache_fingerprint
        with patch.object(prepared_library.time, 'monotonic', side_effect=[0, 601, 601.1]), \
             patch.object(campaign, '_native_cache_fingerprint', wraps=real) as fingerprint:
            first = self.listing()
            second = self.listing()
        self.assertEqual(first['generation'], second['generation'])
        self.assertEqual(fingerprint.call_count, 2)

    def test_ttl_renews_hashes_after_completed_snapshot_age(self):
        real = campaign._native_cache_fingerprint
        with patch.object(prepared_library.time, 'monotonic', side_effect=[0, 10, 310, 320, 330]), \
             patch.object(campaign, '_native_cache_fingerprint', wraps=real) as fingerprint:
            self.listing()
            self.listing()
            self.listing()
        self.assertEqual(fingerprint.call_count, 4)

    def test_physical_marker_change_during_scan_still_rejects_snapshot(self):
        real = campaign._native_cache_fingerprint
        fired = []

        def mutate_after_hash(path):
            result = real(path)
            if not fired:
                fired.append(True)
                self.marker.write_text('{"version":4}', encoding='utf8')
            return result

        with patch.object(campaign, '_native_cache_fingerprint', side_effect=mutate_after_hash):
            with self.assertRaisesRegex(ValueError, 'Acervo mudou'):
                self.listing()
        self.assertEqual(prepared_library._SNAPSHOTS, {})

    def test_partial_and_missing_states_are_physical_records(self):
        self.sensor.unlink()
        self.assertEqual(self.listing()['items'][0]['cache_state'], 'partial')
        self.native.unlink()
        result = self.listing()
        self.assertEqual(result['items'][0]['cache_state'], 'missing')
        self.assertFalse(result['items'][0]['native']['present'])
        self.assertEqual(result['physical_clip_count'], 1)  # orphan marker, not all catalog clips

    def test_source_change_and_legacy_or_malformed_marker_are_stale(self):
        self.source.write_bytes(b'changed original fixture' * 60_000)
        self.assertEqual(self.listing()['items'][0]['cache_state'], 'stale')
        self.assertTrue(self.listing()['items'][0]['source']['present'])
        for marker in ({'version': 4}, {'version': True}, {'version': 5}):
            with self.subTest(marker=marker):
                self.marker.write_text(json.dumps(marker), encoding='utf8')
                self.assertEqual(self.listing()['items'][0]['cache_state'], 'stale')
        self.marker.write_text('{broken', encoding='utf8')
        self.assertEqual(self.listing()['items'][0]['cache_state'], 'stale')

    def test_native_same_size_mtime_mutation_is_detected_by_explicit_refresh(self):
        first = self.listing()
        before = self.native.stat()
        raw = self.native.read_bytes()
        self.native.write_bytes(b'X' + raw[1:])
        os.utime(self.native, ns=(before.st_atime_ns, before.st_mtime_ns))
        result = self.listing(force_refresh=True)
        self.assertEqual(result['items'][0]['cache_state'], 'stale')
        self.assertIn('native_digest_mismatch', result['items'][0]['reasons'])
        self.assertNotEqual(first['generation'], result['generation'])

    def test_marker_change_with_same_stat_invalidates_poll(self):
        first = self.listing()
        before = self.marker.stat()
        text = self.marker.read_text(encoding='utf8')
        text = text.replace('"version": 5', '"version": 4')
        self.marker.write_text(text, encoding='utf8')
        os.utime(self.marker, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertNotEqual(first['generation'], self.listing()['generation'])
        self.assertEqual(self.listing()['items'][0]['cache_state'], 'stale')

    def test_exported_clip_resolves_original_parent_and_window(self):
        source = self.root / (self.CLIP + '.mp4')
        source.write_bytes(b'inert exported fixture' * 60_000)
        self.make_native(self.CLIP, source, None, 180.0)
        item = next(item for item in self.listing()['items'] if item['clip_uid'] == self.CLIP)
        self.assertEqual(item['cache_state'], 'ready')
        self.assertEqual(item['window_s'], [30, 210])
        self.assertEqual(item['source']['name'], self.CLIP + '.mp4')

    def test_synthetic_window_can_use_original_exported_clip_source_offset(self):
        source = self.root / (self.CLIP + '.mp4')
        source.write_bytes(b'inert exported fixture' * 60_000)
        self.make_native(self.uid, source, 0.0, 180.0)
        item = self.listing()['items'][0]
        self.assertEqual(item['cache_state'], 'ready')
        self.assertEqual(item['source']['name'], self.CLIP + '.mp4')

    def test_canonical_uid_quantization_preserves_legitimate_raw_window_cache(self):
        self.make_native(self.uid, self.source, 30.0004, 180.0)
        raw_clip = {'exported_clip_uid': self.uid, 'clip_uid': self.uid,
                    'parent_video_uid': self.PARENT, 'parent_start_sec': '30.0004',
                    'parent_end_sec': '210.0004', 'window_s': [30.0004, 210.0004],
                    'needs_cut': True, 'media_uid': self.PARENT,
                    's3_path': 's3://inert-fixture/source.mp4'}
        with patch.object(campaign, '_ego_clip_inputs', return_value=(raw_clip, {'video_uid': self.PARENT})):
            self.assertEqual(campaign.ego_clip_cache_state(raw_clip, self.root), 'ready')
        self.assertEqual(self.listing()['items'][0]['cache_state'], 'ready')

    def test_canonical_window_rejects_shift_or_end_beyond_uid_quantization(self):
        for start, duration in ((30.002, 180.0), (30.0004, 180.0004), (30, 180.0)):
            with self.subTest(start=start, duration=duration):
                self.make_native(self.uid, self.source, float(start), duration)
                if type(start) is int:
                    # The production marker always writes floats; an integer
                    # replacement cannot be admitted by numeric tolerance.
                    saved = json.loads(self.marker.read_text(encoding='utf8'))
                    saved['start_s'] = start
                    self.marker.write_text(json.dumps(saved), encoding='utf8')
                self.assertEqual(self.listing()['items'][0]['cache_state'], 'stale')

    def test_canonical_window_zero_start_keeps_float_and_none_contract(self):
        uid = self.PARENT + '_0.000_180.000'
        for invalid in (0, None):
            with self.subTest(start=invalid):
                _, marker = self.make_native(uid, self.source, 0.0, 180.0)
                saved = json.loads(marker.read_text(encoding='utf8'))
                saved['start_s'] = invalid
                marker.write_text(json.dumps(saved), encoding='utf8')
                item = next(item for item in self.listing()['items'] if item['clip_uid'] == uid)
                self.assertEqual(item['cache_state'], 'stale')

    def test_unknown_identity_never_uses_marker_to_infer_parent_or_task(self):
        self.make_native('manual-unknown', self.source, 0.0, 180.0)
        item = next(item for item in self.listing()['items'] if item['clip_uid'] == 'manual-unknown')
        self.assertEqual(item['cache_state'], 'stale')
        self.assertIsNone(item['parent_video_uid'])
        self.assertIsNone(item['window_s'])
        self.assertIsNone(item['task_name'])

    def test_actual_pending_journal_protects_ready_media_without_exposing_owner(self):
        journal = self.journal()
        before = journal.read_bytes()
        result = self.listing()
        self.assertEqual(result['protection_status'], 'verified')
        self.assertEqual(result['counts']['protected'], 1)
        self.assertEqual(result['items'][0]['cache_state'], 'ready')
        self.assertTrue(result['items'][0]['protected'])
        self.assertIn('pending_journal', result['items'][0]['protection_reasons'])
        self.assertNotIn('fixture@example.invalid', json.dumps(result))
        self.assertEqual(journal.read_bytes(), before)
        self.assertFalse((self.state / '.media-lifecycle.lock').exists())

    def test_actual_journal_sensor_digest_protects_shared_sensor_and_hashes_it_once(self):
        self.make_native(self.PARENT + '_210.000_390.000', self.source, 210.0, 180.0)
        digest = hashlib.sha256(self.sensor.read_bytes()).hexdigest()
        lineage = {'content': {'assets': {'source_imu': {'sha256': digest}}}}
        lineage['delivery_binding_sha256'] = content_provenance.canonical_digest(lineage)
        row = {'session_id': 'shared-sensor-fixture', 'account_email': 'fixture@example.invalid',
               'org_key': 'fixture-org', 'task_id': 'fixture-task', 'chunk_index': 0,
               'expected_chunk_count': 1, 'state': 'failed', 'phase': 'create',
               'create_attempted': True, 'video_path': str(self.base / 'other-native.mp4'),
               'campaign_context': {'content_provenance': lineage}}
        upload.save_sidecar(row)
        real = campaign._native_cache_fingerprint
        with patch.object(campaign, '_native_cache_fingerprint', wraps=real) as fingerprint:
            result = self.listing()
            self.listing()
        self.assertEqual(result['protection_status'], 'verified')
        self.assertEqual(result['counts']['ready'], 2)
        self.assertEqual(result['counts']['protected'], 2)
        self.assertTrue(all('pending_journal' in item['protection_reasons'] for item in result['items']))
        self.assertEqual(sum(call.args[0] == self.sensor for call in fingerprint.call_args_list), 1)

    def test_digest_reservation_keeps_ambiguous_source_conservatively_protected(self):
        self.make_native('manual-unknown', self.source, 0.0, 180.0)
        lineage = {'content': {'assets': {'source_imu': {'sha256': 'a' * 64}}}}
        lineage['delivery_binding_sha256'] = content_provenance.canonical_digest(lineage)
        self.journal(campaign_context={'content_provenance': lineage})
        item = next(item for item in self.listing()['items'] if item['clip_uid'] == 'manual-unknown')
        self.assertTrue(item['protected'])
        self.assertIn('digest_protection_unverified', item['protection_reasons'])

    def test_corrupt_protection_is_unknown_and_conservatively_protects_all(self):
        journal = self.journal()
        journal.write_bytes(b'{"session_id":"a","session_id":"b"}')
        before = journal.read_bytes()
        result = self.listing()
        self.assertEqual(result['protection_status'], 'unknown')
        self.assertTrue(result['items'][0]['protected'])
        self.assertEqual(journal.read_bytes(), before)

    def test_unmigrated_legacy_journal_is_preserved_without_token_lookup(self):
        directory = self.root.parent / 'sidecars'
        directory.mkdir()
        path = directory / 'legacy.json'
        path.write_text('{}', encoding='utf8')
        with patch.object(token_store, 'records', side_effect=AssertionError('accounts')):
            result = self.listing()
        self.assertEqual(result['protection_status'], 'unknown')
        self.assertTrue(path.exists())
        self.assertFalse(self.state.exists())

    def test_filters_pagination_and_invalid_values(self):
        self.make_native('manual-unknown', self.source, 0.0, 180.0)
        self.assertEqual(self.listing(state='ready', minimum_s=180, maximum_s=180)['total'], 1)
        self.assertEqual(self.listing(query='manual', state='stale')['total'], 1)
        self.assertEqual(len(self.listing(limit=1, offset=1)['items']), 1)
        for kwargs in ({'state': 'protected'}, {'limit': 0}, {'offset': -1},
                       {'minimum_s': float('nan')}, {'minimum_s': True},
                       {'maximum_s': 1, 'minimum_s': 2}, {'force_refresh': 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.listing(**kwargs)

    def test_missing_library_does_not_create_any_directory(self):
        root = self.base / 'not-installed'
        result = prepared_library.list_prepared_clips(root)
        self.assertEqual(result['total'], 0)
        self.assertFalse(root.exists())


if __name__ == '__main__':
    unittest.main()
