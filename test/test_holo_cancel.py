import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from moneymin import campaign, holo_accelerator
from moneymin.web.runner import HoloCacheRunner


class HoloCancelTests(unittest.TestCase):
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
