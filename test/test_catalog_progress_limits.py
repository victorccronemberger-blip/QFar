"""Advancing classification is distinct from a stalled or unbounded job."""
import threading
import unittest
from unittest.mock import patch

from moneymin import background_work
from moneymin.web.catalog_loader import CatalogLoader


class CatalogProgressLimitsTests(unittest.TestCase):
    def test_progress_keeps_one_active_job_but_idle_and_total_limits_remain(self):
        loader = CatalogLoader(timeout_s=300)
        entered, advance, reported, release = (threading.Event() for _ in range(4))
        calls = []
        def work(progress):
            calls.append(1)
            entered.set()
            advance.wait(5)
            background_work.report_progress("Classificando Ego4D: 10/20 vídeos concluídos",
                                            phase="ego4d_annotations")
            reported.set()
            release.wait(5)
            return {"tasks": []}, 200
        try:
            initial, _ = loader.get(("fixture",), work)
            self.assertTrue(entered.wait(1))
            started = loader._jobs[("fixture",)]["started"]
            with patch("moneymin.web.catalog_loader.time.monotonic", return_value=started + 400):
                advance.set()
                self.assertTrue(reported.wait(1))
                body, status = loader.poll(initial["job_id"], key=("fixture",))
                self.assertEqual(status, 202)
                self.assertIn("10/20", body["message"])
                self.assertEqual(body["job_id"], initial["job_id"])
            with patch("moneymin.web.catalog_loader.time.monotonic", return_value=started + 701):
                self.assertEqual(loader.poll(initial["job_id"], key=("fixture",))[1], 504)
            with loader._lock:
                loader._jobs[("fixture",)]["last_progress"] = started + 1800
            with patch("moneymin.web.catalog_loader.time.monotonic", return_value=started + 1800):
                self.assertEqual(loader.poll(initial["job_id"], key=("fixture",))[1], 504)
            self.assertEqual(calls, [1])
        finally:
            advance.set()
            release.set()

    def test_progress_hook_does_not_leak_to_other_operations(self):
        updates = []
        with background_work.responsive_catalog_work(progress=lambda message, **kwargs: updates.append(message)):
            background_work.report_progress("1/2", phase="fixture")
        background_work.report_progress("2/2", phase="fixture")
        self.assertEqual(updates, ["1/2"])
