from __future__ import annotations

import subprocess
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from moneymin import campaign


class EncodeResourceTests(unittest.TestCase):
    def setUp(self):
        campaign._ffmpeg_encoder_available.cache_clear()

    def tearDown(self):
        campaign._ffmpeg_encoder_available.cache_clear()

    def test_listed_but_unusable_gpu_is_rejected_and_result_is_cached(self):
        listed = SimpleNamespace(returncode=0, stdout="h264_nvenc", stderr="")
        failed = SimpleNamespace(returncode=1, stdout="", stderr="No capable devices found")
        with patch.object(campaign, "_ffmpeg_run", side_effect=[listed, failed]) as run:
            self.assertFalse(campaign._ffmpeg_encoder_available("ffmpeg", "h264_nvenc"))
            self.assertFalse(campaign._ffmpeg_encoder_available("ffmpeg", "h264_nvenc"))
        self.assertEqual(run.call_count, 2)

    def test_working_gpu_uses_real_codec_options_for_probe(self):
        ok = SimpleNamespace(returncode=0, stdout="h264_nvenc", stderr="")
        with patch.object(campaign, "_ffmpeg_run", return_value=ok) as run:
            self.assertTrue(campaign._ffmpeg_encoder_available("ffmpeg", "h264_nvenc"))
        command = run.call_args_list[1].args[0]
        self.assertIn("-frames:v", command)
        self.assertEqual(command[command.index("-frames:v") + 1], "1")
        self.assertIn("1440x1080", " ".join(command))
        self.assertEqual(command[-3:], ["-f", "null", "-"])
        codec = campaign._native_video_codec_args(nvenc=True)
        self.assertIn("-spatial-aq", codec)
        self.assertIn("-temporal-aq", codec)
        start = command.index("-c:v")
        self.assertEqual(command[start:start + len(codec)], codec)

    def test_gpu_probe_timeout_selects_cpu(self):
        listed = SimpleNamespace(returncode=0, stdout="h264_nvenc", stderr="")
        with patch.object(campaign, "_ffmpeg_run", side_effect=[
                listed, subprocess.TimeoutExpired("ffmpeg", 20)]):
            self.assertFalse(campaign._ffmpeg_encoder_available("ffmpeg", "h264_nvenc"))

    def test_forced_cpu_skips_gpu_probe(self):
        with patch.dict("os.environ", {"MINUTE_VIDEO_ENCODER": "cpu"}), \
                patch.object(campaign, "_ffmpeg_run") as run:
            self.assertFalse(campaign._use_nvenc("ffmpeg"))
        run.assert_not_called()

    def test_concurrent_multi_slot_reservations_cannot_deadlock(self):
        # Force interleaving after acquisition. The former loop left two
        # callers holding two slots each, both waiting for another two.
        slots = threading.BoundedSemaphore(4)
        barrier = threading.Barrier(2)

        class InterleavedSlots:
            def acquire(self):
                if not slots.acquire(timeout=1):
                    raise TimeoutError("partial resource reservation deadlock")
                try:
                    barrier.wait(timeout=0.05)
                except threading.BrokenBarrierError:
                    pass

            def release(self):
                slots.release()

        errors = []
        completed = []

        def prepare():
            try:
                with campaign._cpu_slots(4):
                    completed.append(True)
            except Exception as exc:
                errors.append(exc)

        with patch.object(campaign, "ACCOUNT_ENCODE_WORKERS", 4), \
                patch.object(campaign, "_ACCOUNT_ENCODE_SLOTS", InterleavedSlots()):
            workers = [threading.Thread(target=prepare) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=3)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertEqual(len(completed), 2)

    def test_failed_work_returns_all_reserved_slots(self):
        slots = threading.BoundedSemaphore(2)
        with patch.object(campaign, "ACCOUNT_ENCODE_WORKERS", 2), \
                patch.object(campaign, "_ACCOUNT_ENCODE_SLOTS", slots):
            with self.assertRaisesRegex(RuntimeError, "encode failed"):
                with campaign._cpu_slots(2):
                    raise RuntimeError("encode failed")
        self.assertTrue(slots.acquire(blocking=False))
        self.assertTrue(slots.acquire(blocking=False))
        self.assertFalse(slots.acquire(blocking=False))


if __name__ == "__main__":
    unittest.main()
