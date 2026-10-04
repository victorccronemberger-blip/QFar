"""Declared local byte/CSV fixtures; no hardware, provider or backend proof."""
import csv
import hashlib
import importlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from moneymin import campaign, sidecar
from moneymin.campaign_types import AccountSpec, CampaignLog


def source_csv():
    return ('canonical_timestamp_ms,gyro_x,gyro_y,gyro_z,accl_x,accl_y,accl_z\n' +
            ''.join(f'{i},1,2,3,4,5,6\n' for i in range(0, 101, 10)))


def output_csv():
    return 't,ax,ay,az,wx,wy,wz\n' + ''.join(f'{i * 1000000},4,5,6,1,2,3\n' for i in range(0, 101, 2))


class ContentProvenanceContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        try:
            self.cp = importlib.import_module('moneymin.content_provenance')
        except ModuleNotFoundError:
            self.cp = None

    def module(self):
        self.assertIsNotNone(self.cp, 'Expected feature gap: R17 has no byte-bound content lineage module')
        return self.cp

    def item(self):
        cp = self.module()
        source = self.root / 'dataset-source.mp4'; source.write_bytes(b'inert-original-media-A')
        imu = self.root / 'dataset-imu.csv'; imu.write_text(source_csv(), encoding='utf-8', newline='')
        prepared = self.root / 'prepared.mp4'; prepared.write_bytes(b'inert-derived-media-B')
        return cp.prepare_content_provenance(source, imu, prepared, output_csv(), 'i,ptsNs,dtNs,tNs,key\n0,0,0,0,1\n',
            clip_uid='clip', parent_video_uid='parent', media_uid='media', window_s=(0.0, 0.1),
            media_offset_s=0.0, normalization_start_s=0.0, selection_evidence=None)

    def test_public_lineage_hashes_same_bytes_and_truthful_grade(self):
        item = self.item(); cp = self.module(); public = item['content_provenance']
        self.assertEqual(public['schema'], 1)
        self.assertFalse(public['physical_provenance_verified'])
        self.assertEqual(public['recording_origin'], 'third_party_dataset')
        self.assertEqual(public['assets']['source_video']['sha256'], hashlib.sha256(b'inert-original-media-A').hexdigest())
        self.assertEqual(public['assets']['imu_csv']['sha256'], hashlib.sha256(output_csv().encode()).hexdigest())
        self.assertNotIn(str(self.root), json.dumps(public))
        cp.revalidate_content_provenance(item)

    def test_same_size_same_mtime_source_replacement_is_rejected(self):
        item = self.item(); cp = self.module()
        for role in ['source_video', 'source_imu', 'prepared_video']:
            with self.subTest(role=role):
                row = item['_content_inputs'][role]; path = Path(row['path']); before = path.read_bytes(); stat = path.stat()
                path.write_bytes(bytes([before[0] ^ 1]) + before[1:])
                import os
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                with self.assertRaises(ValueError): cp.revalidate_content_provenance(item)
                path.write_bytes(before)

    def test_csv_or_window_mutation_is_rejected(self):
        cp = self.module()
        for mutate in [lambda r: r.update(imu_csv=r['imu_csv'] + '\n'),
                       lambda r: r['content_provenance']['window_s'].__setitem__(1, 0.2),
                       lambda r: r['content_provenance']['assets']['source_video'].update(sha256='0' * 64)]:
            with self.subTest(mutation=mutate.__code__.co_firstlineno):
                item = self.item(); mutate(item)
                with self.assertRaises(ValueError): cp.revalidate_content_provenance(item)

    def test_observation_reports_actual_source_gap_output_grid_and_unknown_native(self):
        item = self.item(); observed = item['derived_diagnostics']
        self.assertEqual(observed['source']['gyro']['max_recorded_interval_ns'], 10000000)
        self.assertEqual(observed['source']['accel']['valid_rows'], 11)
        self.assertEqual(observed['output']['sample_count'], 51)
        self.assertEqual(observed['output']['uniform_grid_step_ns'], 2000000)
        self.assertEqual(observed['native_metrics']['status'], 'unknown')
        self.assertIsNone(observed['native_metrics']['maxInterpolationSpanNs'])
        self.assertIsNone(observed['native_metrics']['interpolatedCount'])
        self.assertFalse(observed['native_equivalence_verified'])

    def test_unordered_source_observation_is_consistent(self):
        cp = self.module(); path = self.root / 'imu.csv'; rows = source_csv().splitlines()
        path.write_text('\n'.join([rows[0], *reversed(rows[1:])]) + '\n', encoding='utf-8')
        observation = cp.observe_derived_imu(path, (0, .1), output_csv())
        self.assertEqual(observation['source']['gyro']['max_recorded_interval_ns'], 10000000)
        self.assertEqual(observation['output']['sample_count'], 51)

    def test_sidecar_refuses_unknown_required_native_diagnostics(self):
        observed = self.item()['derived_diagnostics']
        with self.assertRaisesRegex(ValueError, 'derivados'):
            sidecar.build_sidecar_zip_custom(session_id='fixture-sid', chunk_index=0, duration_ms=100,
                recorded_at='2026-10-04T00:00:00.000Z', imu_csv=output_csv(),
                frames_csv='i,ptsNs,dtNs,tNs,key\n0,0,0,0,1\n', derived_diagnostics=observed)

    def test_legacy_call_shape_remains_available(self):
        self.assertIsInstance(sidecar.build_sidecar_zip_custom(session_id='fixture-legacy', chunk_index=0,
            duration_ms=100, recorded_at='2026-10-04T00:00:00.000Z'), bytes)

    def test_actual_upload_blocks_changed_input_before_auth(self):
        cp = self.module(); item = self.item(); item.update(source='ego4d', imu_real=True, clip_uid='clip',
            video_path=item['_content_inputs']['prepared_video']['path'], duration_ms=100)
        Path(item['_content_inputs']['source_video']['path']).write_bytes(b'inert-original-media-Z')
        with patch.object(campaign.Session, 'from_email') as auth, patch.object(campaign, 'upload_session') as upload:
            result = campaign.upload_to_account(item, AccountSpec('fixture@example.invalid', 'fixture-org'),
                'fixture-task', 30, True, True, recover_pending=False)
        self.assertFalse(result['ok']); self.assertFalse(result['retryable']); auth.assert_not_called(); upload.assert_not_called()

    def test_actual_upload_blocks_unsupported_dataset_policy_before_auth(self):
        self.module(); item = self.item(); item.update(source='ego4d', imu_real=True, clip_uid='clip',
            video_path=item['_content_inputs']['prepared_video']['path'], duration_ms=100,
            _content_candidate={'clip_uid': 'clip'}, task_name_authoritative='Declared fixture task')
        # The actual semantic validator is exercised separately by
        # test_content_task_binding; this finite test isolates receiver policy.
        with patch.object(campaign.Session, 'from_email') as auth, patch.object(campaign, 'upload_session') as upload, \
             patch.object(campaign.ego4d, 'revalidate_selection_evidence',
                          return_value={'task': {'name': 'Declared fixture task'}, 'physical_provenance_verified': False}):
            result = campaign.upload_to_account(item, AccountSpec('fixture@example.invalid', 'fixture-org'),
                'fixture-task', 30, True, True, recover_pending=False)
        self.assertFalse(result['ok']); self.assertFalse(result['retryable']); auth.assert_not_called(); upload.assert_not_called()
        self.assertIn('derivados', result['error'])

    def test_delivery_link_and_history_preserve_hashes_without_private_paths(self):
        cp = self.module(); item = self.item(); video = Path(item['_content_inputs']['prepared_video']['path'])
        delivered = cp.bind_content_delivery(item, 'fixture-session', 'fixture-task', 'fixture-org',
            [{'index': 0, 'start_ms': 0, 'duration_ms': 100, 'video_path': str(video),
              'imu_csv': output_csv(), 'frames_csv': 'i,ptsNs,dtNs,tNs,key\n0,0,0,0,1\n', 'sidecar_bytes': b'inert-sidecar'}])
        self.assertEqual(delivered['session_id'], 'fixture-session')
        self.assertEqual(delivered['chunks'][0]['video']['sha256'], hashlib.sha256(video.read_bytes()).hexdigest())
        self.assertFalse(delivered['confirmed_delivery'])
        log = CampaignLog('fixture-time', ['fixture@example.invalid'])
        log.add_item({**item, 'source_provenance': delivered})
        path = log.save(self.root / 'history.json'); history = json.loads(path.read_text())
        self.assertNotIn('_content_inputs', history['items'][0])
        self.assertEqual(history['items'][0]['source_provenance'], delivered)
        self.assertNotIn(str(self.root), json.dumps(history['items'][0]['source_provenance']))


if __name__ == '__main__':
    unittest.main()
