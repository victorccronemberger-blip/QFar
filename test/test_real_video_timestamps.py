"""Actual encoded/decoded MP4s; generated lab imagery, not physical capture."""
from pathlib import Path
import csv
from decimal import Decimal
import io
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from moneymin import sidecar

TOOLS = Path(__file__).resolve().parents[1] / 'dist/QMoney/runtime/tools/ffmpeg/bin'
FFMPEG, FFPROBE = TOOLS/'ffmpeg.exe', TOOLS/'ffprobe.exe'

@unittest.skipUnless(FFMPEG.is_file() and FFPROBE.is_file(), 'Bundled real FFmpeg/ffprobe required')
class RealVideoTimestampsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lab = tempfile.TemporaryDirectory(prefix='qmoney-real-mp4-')
        cls.root = Path(cls.lab.name)
        cls.videos = {}
        variants = {
            'cfr': ['-bf', '0'],
            'cfr_no_edit': ['-bf', '0', '-use_editlist', '0'],
            'all_intra_no_edit': ['-bf', '0', '-g', '1', '-use_editlist', '0'],
            'bframes': ['-bf', '3'],
            'vfr': ['-bf', '0', '-vf', "select='not(mod(n,2))+not(mod(n,5))'", '-fps_mode', 'vfr'],
            'offset': ['-bf', '0', '-vf', 'setpts=PTS+2/TB', '-fps_mode', 'passthrough'],
        }
        for name, flags in variants.items():
            path = cls.root / (name+'.mp4')
            command = [str(FFMPEG), '-nostdin', '-hide_banner', '-loglevel', 'error',
                       '-f','lavfi','-i','testsrc2=size=160x120:rate=30:duration=2',
                       '-an','-c:v','libx264','-preset','ultrafast','-g','15',
                       '-pix_fmt','yuv420p',*flags,'-video_track_timescale','90000',str(path)]
            subprocess.run(command, check=True, capture_output=True, timeout=30)
            cls.videos[name] = path

    @classmethod
    def tearDownClass(cls):
        cls.lab.cleanup()

    def reference(self, name):
        result = subprocess.run([str(FFPROBE),'-v','error','-select_streams','v:0',
             '-show_entries','frame=pts_time,key_frame','-of','json',str(self.videos[name])],
             check=True,capture_output=True,text=True,timeout=30)
        return [(int(Decimal(row['pts_time'])*1_000_000_000),bool(row['key_frame']))
                for row in json.loads(result.stdout)['frames'] if 'pts_time' in row]

    def test_real_decoded_frames_match_reference_for_cfr_vfr_bframes_and_offset(self):
        with patch.object(sidecar,'ffprobe_bin',return_value=str(FFPROBE)):
            for name in self.videos:
                with self.subTest(name=name):
                    expected = self.reference(name)
                    self.assertGreater(len(expected),20)
                    self.assertEqual(sidecar._extract_frame_pts(self.videos[name]),expected)

    def test_real_csv_rebases_only_declared_first_pts_and_preserves_key_flags(self):
        with patch.object(sidecar,'ffprobe_bin',return_value=str(FFPROBE)):
            for name in self.videos:
                with self.subTest(name=name):
                    expected = self.reference(name)
                    rows = list(csv.DictReader(io.StringIO(sidecar.build_frames_csv_from_video(
                        self.videos[name],duration_ms=2000,offset_ns=700_000_000_000,
                        require_measured_pts=True))))
                    self.assertEqual(len(rows),len(expected))
                    for row,(pts,key) in zip(rows,expected):
                        self.assertEqual(int(row['ptsNs']),pts-expected[0][0])
                        self.assertEqual(int(row['tNs']),700_000_000_000+pts-expected[0][0])
                        self.assertEqual(int(row['key']),int(key))

    def test_mp4_fallback_rejects_composition_or_edit_timing_it_cannot_interpret(self):
        with patch.object(sidecar,'_extract_frame_pts_ffprobe',return_value=[]):
            for name in ['bframes','offset']:
                with self.subTest(name=name):
                    self.assertEqual(sidecar._extract_frame_pts(self.videos[name]),[])
                    with self.assertRaises(ValueError):
                        sidecar.build_frames_csv_from_video(self.videos[name],duration_ms=2000,
                                                           require_measured_pts=True)

    def test_variable_rate_fixture_has_actual_unequal_decoded_deltas(self):
        pts = [row[0] for row in self.reference('vfr')]
        self.assertGreater(len(set(b-a for a,b in zip(pts,pts[1:]))),1)

    def test_supported_no_edit_fallback_matches_real_sync_flags_and_time_within_probe_microsecond_precision(self):
        with patch.object(sidecar,'_extract_frame_pts_ffprobe',return_value=[]):
            for name in ['cfr_no_edit','all_intra_no_edit']:
                with self.subTest(name=name):
                    actual = sidecar._extract_frame_pts(self.videos[name])
                    expected = self.reference(name)
                    self.assertEqual(len(actual),len(expected))
                    self.assertEqual([row[1] for row in actual],[row[1] for row in expected])
                    # ffprobe pts_time is printed at microsecond precision.
                    self.assertTrue(all(abs(a[0]-b[0])<=1000 for a,b in zip(actual,expected)))
