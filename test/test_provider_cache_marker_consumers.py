"""Consumer checks with inert media/encoder and real committed marker handling."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from moneymin import campaign, ego_accelerator, holo_accelerator


PAYLOAD = b'\x00\x00\x00\x20ftyp' + b'n' * (1024 * 1024 + 16)
PROBE = {'has_video': True, 'duration_ms': 120000, 'width': 1440,
         'height': 1080, 'fps': 30}


def encode(source, root, *, start=None, duration=None, stem):
    def run(command):
        Path(command[-1]).write_bytes(PAYLOAD)
        return subprocess.CompletedProcess(command, 0, '', '')
    with patch.object(campaign, '_ffmpeg_bin', return_value='inert-encoder'), \
         patch.object(campaign, '_use_nvenc', return_value=False), \
         patch.object(campaign, '_ffmpeg_run', side_effect=run), \
         patch.object(campaign, 'probe_video', return_value=dict(PROBE)):
        return campaign._normalize_video(source, root, start_s=start,
                                         dur_s=duration, stem=stem)


def change_bytes_keep_stat(path):
    stamp = path.stat()
    data = bytearray(path.read_bytes())
    data[-1] ^= 1
    path.write_bytes(data)
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert (path.stat().st_size, path.stat().st_mtime_ns) == (stamp.st_size, stamp.st_mtime_ns)


class ProviderCacheMarkerConsumersTests(unittest.TestCase):
    def ego(self, root):
        row = {'exported_clip_uid': 'clip-fixture', 'parent_video_uid': 'parent-fixture',
               'parent_start_sec': '0', 'parent_end_sec': '120',
               's3_path': 's3://inert-fixture/clip.mp4', 'needs_cut': False}
        plan = campaign._ego_prepare_plan(row)
        source = root / plan['source_name']
        source.write_bytes(b's' * (1024 * 1024 + 24))
        native = encode(source, root, start=plan['norm_start'], duration=plan['dur_s'],
                        stem=row['exported_clip_uid'])
        return row, source, native

    def reclaim(self, root, row):
        with patch.object(ego_accelerator, 'catalog_installed', return_value=True), \
             patch.object(campaign, '_ego_clip_inputs', return_value=(row, {'video_uid': 'parent-fixture'})):
            return ego_accelerator.reclaim_stale_native(root)

    def test_ego_reclaimer_preserves_healthy_pair_and_rejects_changed_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            row, source, native = self.ego(root)
            marker = native.with_name(native.name + '.source.json')
            original = source.read_bytes()
            self.assertEqual(self.reclaim(root, row), {'files': 0, 'bytes': 0})
            self.assertTrue(native.exists() and marker.exists())
            change_bytes_keep_stat(native)
            self.assertEqual(self.reclaim(root, row)['files'], 1)
            self.assertFalse(native.exists() or marker.exists())
            self.assertEqual(source.read_bytes(), original)

    def test_ego_reclaimer_rejects_changed_source_with_same_stat(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            row, source, native = self.ego(root)
            self.assertEqual(self.reclaim(root, row)['files'], 0)
            change_bytes_keep_stat(source)
            self.assertEqual(self.reclaim(root, row)['files'], 1)
            self.assertFalse(native.exists())
            self.assertTrue(source.exists())

    def holo(self, root):
        clip = {'video_name': 'recording-fixture'}
        recording = root / 'recordings' / clip['video_name']
        recording.mkdir(parents=True)
        source = recording / 'Video_compress.mp4'
        source.write_bytes(b's' * (1024 * 1024 + 24))
        for name in holo_accelerator.SENSOR_NAMES:
            sensor = recording / 'IMU' / name
            sensor.parent.mkdir(parents=True, exist_ok=True)
            sensor.write_text('inert presence fixture', encoding='utf-8')
        native = encode(source, root, stem='holoassist_' + clip['video_name'])
        return clip, source, native

    def test_holo_ready_rejects_changed_source_with_same_stat(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clip, source, native = self.holo(root)
            before = native.read_bytes()
            with patch.object(holo_accelerator.holoassist, 'data_dir', return_value=root):
                self.assertTrue(holo_accelerator.clip_ready(clip, root))
                change_bytes_keep_stat(source)
                self.assertFalse(holo_accelerator.clip_ready(clip, root))
            self.assertEqual(native.read_bytes(), before)

    def test_holo_ready_rejects_changed_output_with_same_stat(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clip, source, native = self.holo(root)
            original = source.read_bytes()
            with patch.object(holo_accelerator.holoassist, 'data_dir', return_value=root):
                self.assertTrue(holo_accelerator.clip_ready(clip, root))
                change_bytes_keep_stat(native)
                self.assertFalse(holo_accelerator.clip_ready(clip, root))
            self.assertEqual(source.read_bytes(), original)
