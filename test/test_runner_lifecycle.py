import builtins
import unittest
from unittest.mock import Mock, patch

from moneymin.web import runner


class RunnerLifecycleTests(unittest.TestCase):
    def test_failed_thread_creation_or_start_allows_retry(self):
        for kind in (runner.CampaignRunner, runner.BalancesRunner, runner.HoloCacheRunner):
            for failure in ("construct", "start"):
                with self.subTest(runner=kind.__name__, failure=failure):
                    instance = kind()

                    def start():
                        if kind is runner.CampaignRunner:
                            instance.start(runner.CampaignConfig(accounts=[], tasks=[]))
                        elif kind is runner.BalancesRunner:
                            instance.start({}, Mock())
                        else:
                            instance.start(task="test")

                    target = "moneymin.web.runner.threading.Thread"
                    if failure == "start":
                        target += ".start"
                    with patch(target, side_effect=RuntimeError("private diagnostic")):
                        with self.assertRaisesRegex(RuntimeError, "Não foi possível iniciar"):
                            start()
                    self.assertFalse(instance.running)
                    self.assertEqual(instance.state, "error")
                    self.assertEqual(instance.current, "")
                    self.assertIsNone(instance._thread)
                    self.assertNotIn("private diagnostic", str(getattr(instance, "error", "")))
                    with patch("moneymin.web.runner.threading.Thread.start") as thread_start:
                        start()
                    thread_start.assert_called_once()
                    self.assertTrue(instance.running)

    def test_balance_import_failure_releases_running_state(self):
        instance = runner.BalancesRunner()
        instance.state = "running"
        original_import = builtins.__import__

        def import_module(name, *args, **kwargs):
            if name == "crowtado":
                raise ImportError("missing dependency")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=import_module):
            with self.assertRaises(ImportError):
                instance._run({}, Mock())
        self.assertEqual(instance.state, "error")
        self.assertFalse(instance.running)
