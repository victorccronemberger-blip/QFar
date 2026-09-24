import tempfile
import threading
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from moneymin import campaign, holo_accelerator
from moneymin.web.runner import HoloCacheRunner


class HoloCancelTests(unittest.TestCase):
    def test_ready_requires_native_marker_for_current_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = {"video_name": "fixture"}
            recording = root / "recordings" / "fixture"
            recording.mkdir(parents=True)
            source = recording / "Video_compress.mp4"
            source.write_bytes(b"s" * (1024 * 1024 + 1))
            native = holo_accelerator.native_path(clip, root)
            native.write_bytes(b"n" * (1024 * 1024 + 1))
            for name in holo_accelerator.SENSOR_NAMES:
                sensor = recording / "IMU" / name
                sensor.parent.mkdir(parents=True, exist_ok=True)
                sensor.write_text("data", encoding="utf-8")
            with patch.object(holo_accelerator.holoassist, "data_dir", return_value=root):
                self.assertFalse(holo_accelerator.clip_ready(clip, root))
                marker = native.with_name(native.name + ".source.json")
                marker.write_text(json.dumps(campaign._native_cache_key(
                    source, None, None)), encoding="utf-8")
                self.assertTrue(holo_accelerator.clip_ready(clip, root))
                pitchshift = recording / "Video_pitchshift.mp4"
                pitchshift.write_bytes(b"p" * (1024 * 1024 + 2))
                self.assertFalse(holo_accelerator.clip_ready(clip, root))

    def test_prepared_compressed_video_never_looks_up_remote_pitchshift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recording = root / "recordings" / "fixture"
            recording.mkdir(parents=True)
            compressed = recording / "Video_compress.mp4"
            compressed.write_bytes(b"cached")
            native = root / "native.mp4"
            native.write_bytes(b"native")
            sensors = {
                name: recording / "IMU" / name
                for name in ("Accelerometer_sync.txt", "Gyroscope_sync.txt",
                             "Magnetometer_sync.txt")
            }
            for path in sensors.values():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("cached", encoding="utf-8")
            clip = {"clip_uid": "holoassist:fixture", "video_name": "fixture",
                    "task_type": "assemble stool", "correct_action_ratio": 1.0}
            with patch.object(campaign.holoassist, "data_dir", return_value=root), \
                 patch.object(campaign.holoassist, "download_video") as download, \
                 patch.object(campaign.holoassist, "download_imu") as download_imu, \
                 patch.object(campaign.holoassist, "build_imu_csv", return_value="imu"), \
                 patch.object(campaign, "_normalize_video", return_value=native) as normalize, \
                 patch.object(campaign, "probe_video", return_value={"duration_ms": 300000, "fps": 30}), \
                 patch.object(campaign, "_frames_csv", return_value="frames"):
                result = campaign.prepare_holoassist_clip(clip, root, allow_download=False)
            download.assert_not_called()
            download_imu.assert_not_called()
            self.assertEqual(normalize.call_args.args[0], compressed)
            self.assertEqual(result["source"], "holoassist")

    def test_stop_during_catalog_load_prevents_first_download(self):
        stop = threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def catalogue(*args, **kwargs):
                stop.set()
                (root / "stop").write_text("stop")
                return [{"video_name": "fixture"}]

            with patch.object(holo_accelerator, "eligible_clips", side_effect=catalogue), \
                 patch.object(holo_accelerator, "stop_path", return_value=root / "stop"), \
                 patch.object(holo_accelerator, "state_path", return_value=root / "state.json"), \
                 patch.object(campaign, "prepare_holoassist_clip") as prepare:
                result = holo_accelerator.warm_cache(work_dir=root, should_stop=stop.is_set)
            self.assertEqual(result["status"], "stopped")
            prepare.assert_not_called()

    def test_runner_preserves_stop_before_worker_starts(self):
        runner = HoloCacheRunner()
        with patch("threading.Thread.start"), patch.object(holo_accelerator, "request_stop"):
            runner.start(task="fixture")
            runner.stop()
        with patch.object(holo_accelerator, "warm_cache", return_value={"status": "stopped"}) as warm:
            runner._run(task="fixture")
        self.assertTrue(warm.call_args.kwargs["should_stop"]())
        self.assertEqual(runner.state, "stopped")
        with patch("threading.Thread.start"):
            runner.start(task="fixture")
        self.assertFalse(runner._stop.is_set())
