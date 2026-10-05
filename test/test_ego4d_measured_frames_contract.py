"""Measured-frame requirement: inert media and declared PTS, no hardware claim."""
from __future__ import annotations

from contextlib import ExitStack
import csv
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from moneymin import campaign, sidecar, upload
from moneymin.campaign_types import AccountSpec


DECLARED_PTS = [(100_000_000, True), (140_000_000, False), (210_000_000, True)]
PRIVATE_ERROR = 'Vídeo sem PTS medidos; preparo interrompido.'


class MeasuredFrameHelperTests(unittest.TestCase):
    def test_required_empty_extraction_is_a_private_prepare_error(self):
        with patch.object(sidecar, '_extract_frame_pts', return_value=[]), \
             patch.object(sidecar, 'build_frames_csv') as synthetic:
            with self.assertRaisesRegex(ValueError, '^' + PRIVATE_ERROR.replace('.', r'\.') + '$'):
                sidecar.build_frames_csv_from_video(
                    'private-fixture-path-not-decoded.mp4', duration_ms=2000,
                    require_measured_pts=True)
        synthetic.assert_not_called()

    def test_requirement_flag_requires_an_actual_boolean_before_extraction(self):
        for value in (None, 0, 1, 'true', 'false', [], {}, object()):
            with self.subTest(type=type(value).__name__, value=repr(value)), \
                 patch.object(sidecar, '_extract_frame_pts') as extract:
                with self.assertRaises(ValueError):
                    sidecar.build_frames_csv_from_video(
                        'not-decoded.mp4', duration_ms=2000, require_measured_pts=value)
                extract.assert_not_called()

    def test_extracted_pts_output_bytes_are_unchanged_for_both_boolean_values(self):
        expected = ('i,ptsNs,dtNs,tNs,key\n'
                    '0,0,0,7,1\n'
                    '1,40000000,40000000,40000007,0\n'
                    '2,110000000,70000000,110000007,1\n')
        for value in (False, True):
            with self.subTest(require_measured_pts=value), \
                 patch.object(sidecar, '_extract_frame_pts', return_value=DECLARED_PTS), \
                 patch.object(sidecar, 'build_frames_csv') as synthetic:
                actual = sidecar.build_frames_csv_from_video(
                    'not-decoded.mp4', duration_ms=2000, offset_ns=7,
                    require_measured_pts=value)
                self.assertEqual(actual, expected)
                synthetic.assert_not_called()

    def test_default_and_explicit_false_preserve_legacy_empty_extraction_bytes(self):
        expected = sidecar.build_frames_csv(2000, fps=30.0, gop=30, offset_ns=7)
        with patch.object(sidecar, '_extract_frame_pts', return_value=[]):
            self.assertEqual(sidecar.build_frames_csv_from_video(
                'not-decoded.mp4', duration_ms=2000, fps=30.0, gop=30, offset_ns=7), expected)
            self.assertEqual(sidecar.build_frames_csv_from_video(
                'not-decoded.mp4', duration_ms=2000, fps=30.0, gop=30, offset_ns=7,
                require_measured_pts=False), expected)


class Ego4dCampaignMeasuredFrameTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='ego-measured-frames-inert-')
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.media = self.root / 'declared-inert.mp4'
        self.media.write_bytes(b'Declared inert fixture; no decoded MP4 or physical capture.')
        self.profile = SimpleNamespace(frames_gop=30, uptime_ns_at=lambda _wall: 7)
        self.session = SimpleNamespace(_live=True, _moneymin_pending_pumped=True,
                                       recording_policy=None, email='fixture@example.invalid',
                                       warmup=lambda: None)
        self.account = AccountSpec('fixture@example.invalid', 'declared-org')
        self.sidecars = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # Frame contract only; selection/provenance boundaries are inert here.
        self.stack.enter_context(patch.object(campaign.ego4d,
            'revalidate_selection_evidence', return_value={'schema': 1, 'physical_provenance_verified': False}, create=True))
        self.stack.enter_context(patch.object(campaign.org_policy, 'account_kind', return_value='other'))
        self.stack.enter_context(patch.object(campaign.device_profile, 'get_profile', return_value=self.profile))
        self.stack.enter_context(patch.object(campaign, '_new_identity', return_value=(
            'declared-session', 'declared-session_0', '2026-10-04T00:00:00.000Z')))
        self.stack.enter_context(patch.object(campaign, 'probe_video', side_effect=lambda path: {
            'duration_ms': 60_000, 'fps': 30.0, 'has_video': True, 'width': 1440, 'height': 1080}))
        self.stack.enter_context(patch.object(campaign, '_chunk_video_path', side_effect=lambda _base, index, *_: self.root / f'chunk-{index}.mp4'))
        self.stack.enter_context(patch.object(campaign, '_cut_video_chunk', side_effect=lambda _source, destination, *_: destination.write_bytes(b'Declared inert chunk; not encoded.')))
        self.builder = self.stack.enter_context(patch.object(campaign, 'build_frames_csv_from_video', wraps=sidecar.build_frames_csv_from_video))
        def sidecar_capture(_item, _sid, _log, _recorded, **kwargs):
            self.sidecars.append(kwargs)
            return b'Declared inert sidecar boundary; CSV generation itself is real.'
        self.zip_builder = self.stack.enter_context(patch.object(campaign, '_build_sidecar', side_effect=sidecar_capture))
        self.send = self.stack.enter_context(patch.object(campaign, 'upload_session'))
        self.send.return_value = SimpleNamespace(
            session_id='declared-session', finalized=True, finalize_status=204,
            chunks=[SimpleNamespace(state='done', error=None, upload_id='declared-upload', evaluate_result={})])
        self.create = self.stack.enter_context(patch.object(campaign.Session, 'from_email'))
        self.journal = self.stack.enter_context(patch.object(upload, 'save_sidecar'))
        self.credit = self.stack.enter_context(patch.object(campaign.sent_registry, 'mark_sent'))

    def item(self, source='ego4d', duration_ms=60_000):
        # The data is declared, not acquired. This exercises the finite frame
        # requirement, with auth/ZIP/network boundaries intentionally inert.
        item = {'source': source, 'imu_real': True, 'duration_ms': duration_ms,
                'video_path': str(self.media), 'clip_uid': 'declared-clip',
                'probe': {'duration_ms': duration_ms, 'fps': 30.0},
                'imu_csv': 't,ax,ay,az,wx,wy,wz\n0,1,2,3,4,5,6\n',
                'n_samples': 1, 'require_measured_pts': False}
        if source == 'ego4d':
            from moneymin import content_provenance
            sensor = self.root / 'declared-canonical-imu.csv'
            sensor.write_text('canonical_timestamp_ms,gyro_x,gyro_y,gyro_z,accl_x,accl_y,accl_z\n'
                f'0,4,5,6,1,2,3\n{duration_ms},4,5,6,1,2,3\n', encoding='utf-8')
            item.update(content_provenance.prepare_content_provenance(
                self.media, sensor, self.media, item['imu_csv'], 'i,ptsNs,dtNs,tNs,key\n',
                clip_uid='declared-clip', parent_video_uid='declared-parent', media_uid='declared-media',
                window_s=(0, duration_ms / 1000), media_offset_s=0,
                normalization_start_s=0, selection_evidence=None))
            item['_content_candidate'] = {'clip_uid': 'declared-clip'}
        return item

    def invoke(self, item):
        return campaign.upload_to_account(
            item, self.account, 'declared-task', 30, True, True,
            session=self.session, recover_pending=False)

    def assert_no_publication(self, result):
        self.assertIs(result['ok'], False)
        self.assertIs(result['retryable'], False)
        self.assertIn(PRIVATE_ERROR, result['error'])
        self.assertNotIn(str(self.root), result['error'])
        self.send.assert_not_called()
        self.create.assert_not_called()
        self.journal.assert_not_called()
        self.credit.assert_not_called()

    def test_ego4d_empty_whole_video_frames_cannot_be_relaxed_by_item_flag(self):
        with patch.object(sidecar, '_extract_frame_pts', return_value=[]), \
             patch.object(campaign, '_chunk_plan', return_value=[(0, 60_000)]):
            result = self.invoke(self.item())
        self.assert_no_publication(result)
        self.zip_builder.assert_not_called()

    def test_ego4d_measured_whole_video_reaches_only_inert_upload_boundary(self):
        with patch.object(sidecar, '_extract_frame_pts', return_value=DECLARED_PTS), \
             patch.object(campaign, '_chunk_plan', return_value=[(0, 60_000)]):
            result = self.invoke(self.item())
        self.assertTrue(result['ok'])
        self.assertEqual(self.builder.call_args.kwargs.get('require_measured_pts'), True)
        self.assertEqual(len(self.sidecars), 1)
        rows = list(csv.DictReader(io.StringIO(self.sidecars[0]['frames_csv'])))
        self.assertEqual([int(row['ptsNs']) for row in rows], [0, 40_000_000, 110_000_000])
        self.send.assert_called_once()
        self.journal.assert_not_called()
        self.credit.assert_not_called()

    def test_ego4d_missing_last_chunk_frames_blocks_entire_session_before_send(self):
        def extract(path):
            return [] if Path(path).name == 'chunk-1.mp4' else DECLARED_PTS
        with patch.object(sidecar, '_extract_frame_pts', side_effect=extract), \
             patch.object(campaign, '_chunk_plan', return_value=[(0, 60_000), (60_000, 60_000)]):
            result = self.invoke(self.item(duration_ms=120_000))
        self.assert_no_publication(result)
        self.assertEqual(len(self.sidecars), 1)  # first ZIP is only in memory
        self.assertEqual(self.builder.call_count, 3)  # whole video + two chunks
        self.assertTrue(all(call.kwargs.get('require_measured_pts') is True for call in self.builder.call_args_list))

    def test_ego4d_all_chunk_frames_are_required_without_altering_extracted_pts(self):
        with patch.object(sidecar, '_extract_frame_pts', return_value=DECLARED_PTS), \
             patch.object(campaign, '_chunk_plan', return_value=[(0, 60_000), (60_000, 60_000)]):
            result = self.invoke(self.item(duration_ms=120_000))
        self.assertTrue(result['ok'])
        self.assertEqual(len(self.sidecars), 2)
        self.assertEqual(self.builder.call_count, 3)
        self.assertTrue(all(call.kwargs.get('require_measured_pts') is True for call in self.builder.call_args_list))
        for payload in self.sidecars:
            rows = list(csv.DictReader(io.StringIO(payload['frames_csv'])))
            self.assertEqual([int(row['ptsNs']) for row in rows], [0, 40_000_000, 110_000_000])
        self.send.assert_called_once()

    def test_legacy_source_keeps_default_call_shape_and_synthetic_fallback(self):
        for source in ('holoassist', None):
            with self.subTest(source=source), \
                 patch.object(sidecar, '_extract_frame_pts', return_value=[]), \
                 patch.object(campaign, '_chunk_plan', return_value=[(0, 60_000)]):
                result = self.invoke(self.item(source=source))
                self.assertTrue(result['ok'])
                self.assertNotIn('require_measured_pts', self.builder.call_args.kwargs)
                self.assertTrue(self.sidecars[-1]['frames_csv'].startswith('i,ptsNs,dtNs,tNs,key\n'))
                self.send.reset_mock()


if __name__ == '__main__':
    unittest.main()
