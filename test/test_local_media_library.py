"""General local storage: inert media bytes, real NTFS identities/journals."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from moneymin import (campaign, config, content_provenance, ego4d, prepared_library,
                      token_store, upload, upload_storage)


class LocalMediaLibraryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temporary = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='local-media-library-'))
        self.base = Path(temporary)
        self.root = self.base / 'media'
        self.root.mkdir()
        self.state = self.base / 'state'
        self.stack.enter_context(patch.multiple(config, DATA_DIR=self.state,
                                               MEDIA_DATA_DIR=self.root,
                                               SECRETS_DIR=self.base / 'secrets'))

    def file(self, relative, raw=b'inert local media'):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path

    def listing(self, **kwargs):
        return prepared_library.list_local_media(self.root, **kwargs)

    def journal(self, video, **extra):
        row = {'session_id': 'local-media-fixture', 'account_email': 'fixture@example.invalid',
               'org_key': 'fixture-org', 'task_id': 'fixture-task', 'chunk_index': 0,
               'expected_chunk_count': 1, 'state': 'failed', 'phase': 'create',
               'create_attempted': True, 'video_path': str(video)}
        row.update(extra)
        return upload.save_sidecar(row)

    def test_source_and_imu_without_native_are_listed_without_task_approval(self):
        video = self.file('ego4d/parent.mp4', b'original source')
        sensor = self.file('ego4d/parent_imu.csv', b'inert sensor presence')
        result = self.listing()
        self.assertEqual(result['inventory_scope'], 'local_media_files')
        self.assertEqual(result['file_count'], 2)
        self.assertEqual(result['total_bytes'], video.stat().st_size + sensor.stat().st_size)
        self.assertEqual({item['kind'] for item in result['items']}, {'video', 'sensor'})
        self.assertEqual({item['stage'] for item in result['items']}, {'baixado/original', 'sensores'})
        self.assertTrue(all('task_name' not in item and 'selection_evidence' not in item for item in result['items']))

    def test_provider_holo_prefix_precedes_ego_folder_and_unknown_root_is_local(self):
        self.file('ego4d/holoassist_recording_native.mp4')
        self.file('holoassist/recordings/recording/video.mp4')
        self.file('nymeria/sequence/video.vrs')
        self.file('local-camera.mov')
        result = self.listing()
        self.assertEqual(result['by_provider']['holoassist']['file_count'], 2)
        self.assertEqual(result['by_provider']['nymeria']['file_count'], 1)
        self.assertEqual(result['by_provider']['local']['file_count'], 1)
        item = next(item for item in result['items'] if item['name'].startswith('holoassist_'))
        self.assertEqual(item['kind'], 'video')
        self.assertEqual(item['stage'], 'normalizado')

    def test_original_recording_source_and_sensor_are_local_storage(self):
        video = self.file('original/recording/video.mp4', b'original recording')
        sensor = self.file('original/recording/imu.csv', b'original sensor')
        result = self.listing(provider='local')
        self.assertEqual(result['file_count'], 2)
        self.assertEqual(result['total_bytes'], video.stat().st_size + sensor.stat().st_size)
        self.assertEqual({item['kind'] for item in result['items']}, {'video', 'sensor'})
        self.assertTrue(all(item['provider'] == 'local' for item in result['items']))

    def test_original_catalogs_derivatives_and_raw_downloads_remain_visible(self):
        for name in ('ego4d.json', 'clips.csv', 'timed_narrations.jsonl'):
            self.file('ego4d/' + name)
        self.file('ego4d/source.mp4.part')
        self.file('ego4d/source_native.mp4.source.json')
        self.file('nymeria/sequence/metadata.json')
        self.file('nymeria/sequence/raw/custom-stream.bin')
        self.file('videos/session/frames.csv')
        result = self.listing()
        kinds = {item['relative_path']: item['kind'] for item in result['items']}
        self.assertEqual(kinds['ego4d/ego4d.json'], 'catalog')
        self.assertEqual(kinds['ego4d/source.mp4.part'], 'video')
        self.assertEqual(kinds['ego4d/source_native.mp4.source.json'], 'derivative')
        self.assertEqual(kinds['nymeria/sequence/metadata.json'], 'catalog')
        self.assertEqual(kinds['nymeria/sequence/raw/custom-stream.bin'], 'derivative')
        self.assertEqual(kinds['videos/session/frames.csv'], 'derivative')

    def test_private_stores_are_pruned_even_inside_media_trees(self):
        self.file('secrets/token_fixture.json')
        self.file('config/settings.json')
        self.file('ego4d/secrets/account.mp4')
        self.file('ego4d/device_state/profile.mov')
        self.file('ego4d/stores/private/video.mp4')
        self.file('ego4d/campaign_private.json')
        self.file('ego4d/token_fixture.json')
        self.file('webui_prefs.json')
        self.file('campaign_demo.mp4')
        self.file('ego4d/account_garden.mp4')
        result = self.listing()
        self.assertEqual({item['name'] for item in result['items']}, {'campaign_demo.mp4', 'account_garden.mp4'})

    def test_121_files_are_not_capped_by_campaign_or_page_limit(self):
        for index in range(121):
            self.file(f'ego4d/source-{index:03d}.mp4', b'one')
        first = self.listing(limit=100)
        second = self.listing(limit=100, offset=100)
        self.assertEqual(first['file_count'], 121)
        self.assertEqual(first['total'], 121)
        self.assertEqual(first['total_bytes'], 121 * 3)
        self.assertEqual(len(first['items']), 100)
        self.assertEqual(len(second['items']), 21)
        self.assertFalse({item['path'] for item in first['items']} & {item['path'] for item in second['items']})

    def test_hardlinks_deduplicate_bytes_and_union_pending_alias_path_protection(self):
        first = self.file('a.mp4', b'one physical source')
        second = self.root / 'b.mp4'
        os.link(first, second)
        journal = self.journal(second)
        before = journal.read_bytes()
        result = self.listing()
        self.assertEqual(result['file_count'], 1)
        self.assertEqual(result['total_bytes'], first.stat().st_size)
        self.assertEqual(result['items'][0]['name'], 'a.mp4')
        self.assertTrue(result['items'][0]['protected'])
        self.assertIn('pending_journal', result['items'][0]['protection_reasons'])
        self.assertEqual(journal.read_bytes(), before)
        self.assertNotIn('fixture@example.invalid', json.dumps(result))

    def test_corrupt_protection_stays_unknown_without_modifying_journal(self):
        video = self.file('ego4d/source.mp4')
        journal = self.journal(video)
        journal.write_bytes(b'{"session_id":"a","session_id":"b"}')
        before = journal.read_bytes()
        result = self.listing()
        self.assertEqual(result['protection_status'], 'unknown')
        self.assertTrue(result['items'][0]['protected'])
        self.assertEqual(journal.read_bytes(), before)

    def test_digest_protection_is_conservative_without_hashing_any_media(self):
        video = self.file('ego4d/source.mp4')
        lineage = {'content': {'assets': {'source_video': {'sha256': 'a' * 64}}}}
        lineage['delivery_binding_sha256'] = content_provenance.canonical_digest(lineage)
        self.journal(video, campaign_context={'content_provenance': lineage})
        with patch.object(campaign, '_native_cache_fingerprint', side_effect=AssertionError('media hash')):
            result = self.listing()
        self.assertTrue(result['items'][0]['protected'])
        self.assertIn('digest_protection_unverified', result['items'][0]['protection_reasons'])

    def test_no_media_reads_accounts_download_probe_preparation_or_writes(self):
        self.file('ego4d/source.mp4')
        self.file('ego4d/source_imu.csv')
        self.file('ego4d/ego4d.json', b'not parsed by presence inventory')
        before = {path: path.read_bytes() for path in self.base.rglob('*') if path.is_file()}
        real_open = Path.open

        def guarded_open(path, mode='r', *args, **kwargs):
            if any(character in mode for character in 'wax+'):
                raise AssertionError('write')
            if path.resolve().is_relative_to(self.root):
                raise AssertionError('media contents read')
            return real_open(path, mode, *args, **kwargs)

        with patch.object(Path, 'open', guarded_open), \
             patch.object(prepared_library, '_scan', side_effect=AssertionError('prepared scan')), \
             patch.object(ego4d, '_s3', side_effect=AssertionError('network')), \
             patch.object(ego4d, 'download_clip', side_effect=AssertionError('download')), \
             patch.object(campaign, 'probe_video', side_effect=AssertionError('probe')), \
             patch.object(campaign, 'prepare_clip', side_effect=AssertionError('prepare')), \
             patch.object(campaign, '_native_cache_fingerprint', side_effect=AssertionError('hash')), \
             patch.object(token_store, 'records', side_effect=AssertionError('accounts')), \
             patch.object(upload_storage, 'journal_directory', side_effect=AssertionError('migration')):
            self.assertEqual(self.listing()['file_count'], 3)
        self.assertFalse(self.state.exists())
        self.assertEqual(before, {path: path.read_bytes() for path in self.base.rglob('*') if path.is_file()})

    @unittest.skipUnless(os.name == 'nt', 'Windows junction fixture')
    def test_windows_junction_escape_and_loop_are_pruned_before_recursion(self):
        self.file('ego4d/source.mp4')
        outside = self.base / 'outside-media'
        outside.mkdir()
        sentinel = outside / 'excluded.mp4'
        sentinel.write_bytes(b'outside original fixture')
        links = []
        try:
            for name, target in (('escape', outside), ('loop', self.root / 'ego4d')):
                junction = self.root / 'ego4d' / name
                result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(target)],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self.assertEqual(result.returncode, 0)
                links.append(junction)
            result = self.listing()
            self.assertEqual(result['file_count'], 1)
            self.assertEqual(result['scan_errors'], 0)
            self.assertEqual(sentinel.read_bytes(), b'outside original fixture')
        finally:
            for junction in links:
                os.rmdir(junction)  # Removes the link itself, never its target.

    def test_filters_and_pagination_preserve_global_physical_totals(self):
        self.file('ego4d/source.mp4', b'video')
        self.file('ego4d/source_imu.csv', b'sensor')
        self.file('holoassist/video.mp4', b'holo')
        result = self.listing(provider='ego4d', kind='video', query='source')
        self.assertEqual(result['file_count'], 3)
        self.assertEqual(result['total_bytes'], 15)
        self.assertEqual(result['total'], 1)
        self.assertEqual(len(self.listing(limit=1, offset=2)['items']), 1)
        for kwargs in ({'provider': 'minute'}, {'kind': 'task'}, {'limit': 0}, {'limit': True},
                       {'offset': -1}, {'query': 'x' * 201}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.listing(**kwargs)

    def test_missing_root_is_read_only_empty_storage(self):
        missing = self.base / 'missing-media'
        result = prepared_library.list_local_media(missing)
        self.assertEqual(result['file_count'], 0)
        self.assertFalse(missing.exists())
        self.assertFalse(self.state.exists())


if __name__ == '__main__':
    unittest.main()
