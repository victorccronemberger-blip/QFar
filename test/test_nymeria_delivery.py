"""Offline VFR recoding/cut and independent sensor-origin regressions."""
import csv
import io
from pathlib import Path
import shutil
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from moneymin import campaign, content_provenance, nymeria_vrs, sidecar
from test_nymeria_timestamps import FakeProvider, imu_samples, sdk_patch


@unittest.skipUnless(Path(sidecar.ffmpeg_bin()).is_file() or shutil.which(sidecar.ffmpeg_bin()), 'FFmpeg required')
class NymeriaDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='nymeria-delivery-')
        self.root = Path(self.temporary.name)
        self.vrs = self.root / 'data.vrs'
        self.vrs.write_bytes(b'inert image provider identity')

    def tearDown(self):
        self.temporary.cleanup()

    def video(self):
        rgb = np.full((12, 20, 3), 110, dtype=np.uint8)
        provider = FakeProvider(images=[(t, rgb) for t in (0, 31_000_000, 71_000_000, 121_000_000)])
        output = self.root / 'irregular.mp4'
        with sdk_patch(), patch.object(nymeria_vrs, '_provider', return_value=provider):
            nymeria_vrs.extract_rgb_mp4(self.vrs, output, t0_ns=0, t1_ns=160_000_000)
        return output

    def test_account_encode_retains_actual_pts_and_last_packet_duration(self):
        source = self.video()
        profile = SimpleNamespace(device_id='abcdef01-2345', video_bitrate_mbps=1., frames_gop=30)
        with patch.object(campaign, '_use_nvenc', return_value=False):
            output = campaign._per_account_video(source, profile, preserve_pts=True)
        self.assertEqual(campaign._measured_video_pts(output), [0, 31_000_000, 71_000_000, 121_000_000])
        self.assertEqual(sidecar.probe_video(output)['duration_ms'], 160)
        self.assertEqual(campaign._account_video_ok_path(output).read_text(), '4-vfr1')

    def test_cut_uses_first_actual_frame_after_boundary_without_duplication(self):
        source = self.video()
        output = campaign._chunk_video_path(source, 1, 30, 130, preserve_pts=True)
        campaign._cut_video_chunk(source, output, .03, .13, preserve_pts=True)
        self.assertEqual(campaign._measured_video_pts(output), [0, 40_000_000, 90_000_000])
        self.assertEqual(sidecar.probe_video(output)['duration_ms'], 129)
        self.assertEqual(campaign._require_same_video_pts(source, output, start_ns=30_000_000,
                                                         end_ns=160_000_000), 31_000_000)

    def test_chunk_resamples_at_actual_frame_origin_instead_of_nominal_boundary(self):
        motion = self.root / 'motion.vrs'
        motion.write_bytes(b'inert sensor provider identity')
        inputs = {'source_video': content_provenance.fingerprint(self.vrs),
                  'source_imu': content_provenance.fingerprint(motion)}
        transform = {'first_rgb_capture_ns': '5000000', 'source_device_window_ns': [0, 125_000_000]}
        item = {'duration_ms': 120, '_content_inputs': inputs, 'content_provenance': {'transform': transform},
                '_nymeria_resample_inputs': {'motion_vrs': inputs['source_imu'], 'data_vrs': inputs['source_video'],
                    'origin_ns': 5_000_000, 'duration_ms': 120, 'sample_rate_hz': 500,
                    'source_device_window_ns': [0, 125_000_000], 'imu_label': 'imu-right'}}
        provider = FakeProvider(imu=[(ts, acc, gyro, True) for ts, acc, gyro in imu_samples(0, 150_000_000)])
        with sdk_patch(), patch.object(nymeria_vrs, '_provider', return_value=provider):
            full, _ = campaign._nymeria_resample_delivery(item)
            part, diagnostics = campaign._nymeria_resample_delivery(item, origin_ns=36_000_000, duration_ms=89)
        first = next(csv.DictReader(io.StringIO(part)))
        nominal = next(csv.DictReader(io.StringIO(campaign._slice_imu_csv(full, 30, 89)[0])))
        self.assertEqual(first['t'], '0')
        self.assertEqual(float(first['ax']), 36.)
        self.assertEqual(float(nominal['ax']), 35.)
        self.assertEqual(diagnostics['sampleCount'], 45)
        self.assertEqual(diagnostics['sourceOriginNs'], '36000000')

    def test_legacy_nymeria_item_is_rejected_before_session_authentication(self):
        item = {'source': 'nymeria', 'imu_real': True, 'imu_csv': 'legacy', 'frames_csv': 'legacy'}
        account = SimpleNamespace(email='offline@example.invalid', org_key='local-audit')
        with patch.object(campaign.Session, 'from_email') as session, patch.object(campaign, 'upload_session') as upload:
            result = campaign.upload_to_account(item, account, task_id='local-only',
                                                timeout_blob=1, evaluate=False, finalize=False)
        self.assertFalse(result['ok'])
        session.assert_not_called()
        upload.assert_not_called()


if __name__ == '__main__':
    unittest.main()
