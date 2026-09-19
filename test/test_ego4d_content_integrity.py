import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from moneymin import ego4d, sidecar


class Ego4dContentIntegrityTests(unittest.TestCase):
    def test_invalid_requested_window_never_downloads_entire_parent(self):
        with patch.object(ego4d, "_download_to") as download:
            for end in (0, -1, float("nan")):
                with self.subTest(end=end), self.assertRaises(ValueError):
                    ego4d.download_clip({"needs_cut": True, "parent_start_sec": 0,
                                         "parent_end_sec": end, "s3_path": "s3://bucket/video"}, Path("out.mp4"))
            download.assert_not_called()

    def test_orphan_clip_is_rejected_even_without_imu_filter(self):
        self.assertFalse(ego4d._passes_filter({"parent_video_uid": "missing",
                         "parent_start_sec": "0", "parent_end_sec": "60"}, {}, None, False, None, 1, 100))

    def test_short_http_download_is_rejected(self):
        response = io.BytesIO(b"short")
        response.headers = {"Content-Length": "100"}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(ego4d, "_aws_creds", return_value=("fake", "fake", None)), \
             patch.object(ego4d, "_aws_region", return_value="us-east-1"), \
             patch.object(ego4d.tls, "urlopen", return_value=response):
            with self.assertRaisesRegex(OSError, "incompleto"):
                ego4d._s3_get_stdlib("bucket", "key", Path(directory) / "download.part")

    def test_cut_timeout_preserves_destination_and_removes_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dest = root / "clip.mp4"
            dest.write_bytes(b"original")
            def run(command, **kwargs):
                Path(command[-1]).write_bytes(b"partial")
                raise subprocess.TimeoutExpired(command, 3600)
            with patch.object(sidecar, "ffmpeg_bin", return_value="ffmpeg"), \
                 patch("subprocess.run", side_effect=run), self.assertRaises(subprocess.TimeoutExpired):
                ego4d._extract_window(root / "source.mp4", 0, 2, dest)
            self.assertEqual(dest.read_bytes(), b"original")
            self.assertEqual(list(root.iterdir()), [dest])

    def test_invalid_cut_window_never_starts_ffmpeg(self):
        with patch("subprocess.run") as run:
            for start, duration in ((-1, 2), (0, 0), (float("nan"), 1), (0, float("inf"))):
                with self.subTest(start=start, duration=duration), self.assertRaises(ValueError):
                    ego4d._extract_window(Path("source.mp4"), start, duration, Path("out.mp4"))
            run.assert_not_called()

    def test_mp4_header_alone_is_not_valid_video(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"\x00\x00\x00\x20ftyp" + b"0" * 2048)
            for info in ({}, {"has_video": False}, {"has_video": True, "duration_ms": 0},
                         {"has_video": True, "duration_ms": 1000, "width": 0, "height": 100}):
                with self.subTest(info=info), patch.object(sidecar, "probe_video", return_value=info):
                    self.assertFalse(ego4d._valid_mp4_cache(path))
            with patch.object(sidecar, "probe_video", return_value={
                    "has_video": True, "duration_ms": 1000, "width": 100, "height": 100}):
                self.assertTrue(ego4d._valid_mp4_cache(path))

    def test_invalid_download_never_replaces_existing_file(self):
        resource = Mock()
        def download(key, target, **kwargs):
            Path(target).write_bytes(b"invalid replacement")
        resource.Bucket.return_value.download_file.side_effect = download
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"original")
            with patch.object(ego4d, "_s3", return_value=resource), self.assertRaises(RuntimeError):
                ego4d._download_to("bucket", "key", path, validator=lambda _: False)
            self.assertEqual(path.read_bytes(), b"original")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_catalog_staging_names_are_unique(self):
        names = []
        def download(bucket, key, dest):
            names.append(dest)
            dest.write_text("data")
        with tempfile.TemporaryDirectory() as directory, patch.object(ego4d, "_download_to", side_effect=download):
            path = Path(directory) / "catalog.json"
            for _ in range(2):
                ego4d._refresh_catalog_file("bucket", "key", path, lambda _: True)
            self.assertNotEqual(names[0], names[1])
            self.assertEqual(list(Path(directory).iterdir()), [path])
