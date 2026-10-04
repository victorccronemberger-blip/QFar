import csv
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts.index_ego4d_library import index_library, search_library, audit_sensor
from moneymin import campaign


class OriginalLibraryTests(unittest.TestCase):
    def test_sensor_audit_reports_native_rates_and_actual_missing_samples(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'imu.csv'
            path.write_text('canonical_timestamp_ms,gyro_x,gyro_y,gyro_z,accl_x,accl_y,accl_z\n'
                            '0,1,2,3,4,5,6\n10,1,2,3,,,\n20,1,2,3,4,5,6\n')
            original = path.read_bytes()
            report = audit_sensor(path)['sensors']
            self.assertEqual(report['gyroscope']['mean_observed_rate_hz'], 100)
            self.assertEqual(report['accelerometer']['mean_observed_rate_hz'], 50)
            self.assertEqual(report['accelerometer']['missing_or_invalid_rows'], 1)
            self.assertEqual(report['accelerometer']['maximum_gap_ms'], 20)
            self.assertEqual(original, path.read_bytes())

    def source(self, root):
        (root / 'ego4d.json').write_text(json.dumps({'version': 'fixture', 'videos': [
            {'video_uid': 'v1', 'duration_sec': 600.123, 'scenarios': ['Cooking'], 'has_imu': False},
            {'video_uid': 'v2', 'duration_sec': 90, 'scenarios': ['Gardening']},
        ]}), encoding='utf-8')
        with (root / 'clips.csv').open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=[
                'exported_clip_uid', 'parent_video_uid', 'parent_start_sec', 'parent_end_sec'])
            writer.writeheader()
            for uid, parent, start, end in [('c1', 'v1', 10.123, 50.456),
                                           ('c2', 'v2', 0, 100), ('c3', 'missing', 0, 20)]:
                writer.writerow(dict(zip(writer.fieldnames, (uid, parent, start, end))))
        (root / 'timed_narrations.jsonl').write_text(json.dumps({
            'video_uid': 'v1', 'events': [[20, '#C stirs soup'], [800, '#C walks']]}))

    def test_complete_inventory_preserves_source_duration_and_unknown_sensor_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.source(root)
            report = index_library(root, root / 'library.sqlite3')
            self.assertEqual(report['videos'], 2)
            self.assertEqual(report['issues'], {'invalid_clip_window': 2, 'invalid_annotation': 1})
            hits = search_library(root / 'library.sqlite3', 'soup')
            self.assertEqual(hits[0]['duration_s'], 600.123)
            self.assertEqual(hits[0]['has_imu'], 0)
            self.assertIsNone(search_library(root / 'library.sqlite3', 'Gardening')[0]['has_imu'])
            with closing(sqlite3.connect(root / 'library.sqlite3')) as db:
                self.assertAlmostEqual(db.execute('SELECT duration_s FROM clip WHERE uid="c1"').fetchone()[0], 40.333)
                self.assertIsNone(db.execute('SELECT duration_s FROM clip WHERE uid="c2"').fetchone()[0])
                self.assertEqual(db.execute('SELECT COUNT(*) FROM source_file').fetchone()[0], 3)

    def test_invalid_rebuild_preserves_previous_index_and_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.source(root)
            output = root / 'library.sqlite3'
            index_library(root, output)
            previous = output.read_bytes()
            (root / 'timed_narrations.jsonl').write_text('broken-json')
            with self.assertRaises(ValueError):
                index_library(root, output)
            self.assertEqual(previous, output.read_bytes())
            self.assertEqual(list(root.glob('*.tmp')), [])

    def test_prepare_plan_preserves_exact_original_window(self):
        plan = campaign._ego_prepare_plan({'exported_clip_uid': 'c1', 'parent_video_uid': 'v1',
                                          'parent_start_sec': '10.123', 'parent_end_sec': '310.123'})
        self.assertEqual(plan['window_s'], (10.123, 310.123))
        self.assertEqual(plan['dur_s'], 300)

    def test_encoded_duration_divergence_is_rejected_before_sensor_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            native = root / 'native.mp4'
            native.write_bytes(b'fixture')
            # Declared bytes/CSV make the new lineage checks meaningful; all
            # acquisition/encoder/probe boundaries remain inert in this test.
            (root / 'c1.mp4').write_bytes(b'declared original fixture')
            (root / 'imu.csv').write_text(
                'canonical_timestamp_ms,gyro_x,gyro_y,gyro_z,accl_x,accl_y,accl_z\n'
                '0,1,2,3,4,5,6\n300000,1,2,3,4,5,6\n', encoding='utf-8')
            clip = {'exported_clip_uid': 'c1', 'parent_video_uid': 'v1',
                    'parent_start_sec': '0', 'parent_end_sec': '300'}
            with patch.object(campaign.ego4d, 'imu_window_is_covered', return_value=True), \
                 patch.object(campaign.ego4d, 'download_imu', return_value=root / 'imu.csv'), \
                 patch.object(campaign.ego4d, 'download_clip'), \
                 patch.object(campaign.ego4d, 'build_imu_csv', return_value='') as sensors, \
                 patch.object(campaign, '_normalize_video', return_value=native), \
                 patch.object(campaign, 'probe_video', return_value={'duration_ms': 298000, 'fps': 30}):
                with self.assertRaisesRegex(RuntimeError, 'divergente'):
                    campaign.prepare_clip(clip, {'has_imu': True}, root)
                self.assertEqual(sensors.call_count, 1)  # preflight only

    def test_wrong_parent_metadata_is_rejected_before_download_or_processing(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(campaign.ego4d, 'download_imu') as download, \
             patch.object(campaign.ego4d, 'build_imu_csv') as sensors:
            with self.assertRaisesRegex(RuntimeError, 'pai original'):
                campaign.prepare_clip({'exported_clip_uid': 'c1', 'parent_video_uid': 'v1',
                                       'parent_start_sec': '0', 'parent_end_sec': '300'},
                                      {'video_uid': 'wrong-parent', 'has_imu': True}, Path(folder))
            download.assert_not_called()
            sensors.assert_not_called()

    def test_missing_parent_is_not_guessed_from_the_clip_identity(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(campaign.ego4d, 'download_imu') as download:
            with self.assertRaisesRegex(RuntimeError, 'identidade do pai original'):
                campaign.prepare_clip({'exported_clip_uid': 'c1',
                                       'parent_start_sec': '0', 'parent_end_sec': '300'},
                                      {'has_imu': True}, Path(folder))
            download.assert_not_called()

    def test_prepared_item_preserves_dataset_origin_without_claiming_native_rate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            native = root / 'native.mp4'
            native.write_bytes(b'fixture')
            (root / 'c1.mp4').write_bytes(b'declared original fixture')
            (root / 'v1_imu.csv').write_text(
                'canonical_timestamp_ms,gyro_x,gyro_y,gyro_z,accl_x,accl_y,accl_z\n'
                '10123,1,2,3,4,5,6\n310123,1,2,3,4,5,6\n', encoding='utf-8')
            clip = {'exported_clip_uid': 'c1', 'parent_video_uid': 'v1',
                    'parent_start_sec': '10.123', 'parent_end_sec': '310.123'}
            video = {'video_uid': 'v1', 'device': 'original-camera', 'has_imu': True}
            original_clip, original_video = dict(clip), dict(video)
            with patch.object(campaign.ego4d, 'imu_window_is_covered', return_value=True), \
                 patch.object(campaign.ego4d, 'download_imu', return_value=root / 'v1_imu.csv'), \
                 patch.object(campaign.ego4d, 'download_clip'), \
                 patch.object(campaign.ego4d, 'build_imu_csv', return_value=
                    't,ax,ay,az,wx,wy,wz\n0,4,5,6,1,2,3\n300000000000,4,5,6,1,2,3\n'), \
                 patch.object(campaign, '_normalize_video', return_value=native), \
                 patch.object(campaign, 'probe_video', return_value={'duration_ms': 300000, 'fps': 30}):
                item = campaign.prepare_clip(clip, video, root)
            origin = item['source_provenance']
            self.assertEqual(origin['recording_origin'], 'third_party_dataset')
            self.assertEqual(origin['parent_video_uid'], 'v1')
            self.assertTrue(origin['parent_identity_verified'])
            self.assertEqual(origin['window_s'], [10.123, 310.123])
            self.assertEqual(origin['original_device'], 'original-camera')
            self.assertEqual(origin['prepared_duration_ms'], item['duration_ms'])
            self.assertEqual(origin['imu']['processing'], 'resampled_measured_signals')
            self.assertIsNone(origin['imu']['native_rate_hz'])
            self.assertEqual(clip, original_clip)
            self.assertEqual(video, original_video)
