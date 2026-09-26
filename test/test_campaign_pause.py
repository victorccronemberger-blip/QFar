import threading
import unittest
from unittest.mock import patch

from moneymin.web import runner, server


class CampaignPauseTests(unittest.TestCase):
    def setUp(self):
        self.instance = runner.CampaignRunner()
        self.instance.state = "running"

    def test_pause_blocks_next_checkpoint_and_resume_releases_it(self):
        self.instance.pause()
        entered, finished = threading.Event(), threading.Event()
        def step():
            entered.set()
            self.assertFalse(self.instance._checkpoint())
            finished.set()
        worker = threading.Thread(target=step, daemon=True)
        worker.start()
        self.assertTrue(entered.wait(1))
        self.assertFalse(finished.wait(0.05))
        self.assertTrue(self.instance.snapshot()["pause_requested"])
        self.assertTrue(self.instance.running)
        self.instance.resume()
        self.assertTrue(finished.wait(1))
        worker.join(1)
        self.assertFalse(self.instance.snapshot()["pause_requested"])

    def test_stop_releases_paused_checkpoint_and_requests_cancellation(self):
        self.instance.pause()
        result = []
        worker = threading.Thread(target=lambda: result.append(self.instance._checkpoint()), daemon=True)
        worker.start()
        self.instance.stop()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [True])
        self.assertEqual(self.instance.state, "stopping")

    def test_invalid_state_does_not_pause_or_resume(self):
        for state in ("idle", "done", "error", "stopping", "stopped"):
            self.instance.state = state
            with self.assertRaises(RuntimeError):
                self.instance.pause()
            with self.assertRaises(RuntimeError):
                self.instance.resume()

    def test_api_reports_pause_resume_and_conflict(self):
        with patch.object(server, "RUNNER", self.instance):
            client = server.create_app().test_client()
            self.assertEqual(client.post("/api/campaigns/pause").status_code, 200)
            self.assertTrue(client.get("/api/campaigns/current").get_json()["pause_requested"])
            self.assertEqual(client.post("/api/campaigns/resume").status_code, 200)
            self.assertEqual(client.post("/api/campaigns/resume").status_code, 409)

    def test_completion_clears_pause_request(self):
        def engine(cfg, progress, should_stop):
            self.instance.pause()
            progress("campaign_done", {"status": "done"})
        with patch.object(runner, "run_campaign", side_effect=engine):
            self.instance._run(runner.CampaignConfig(accounts=[], tasks=[]))
        self.assertEqual(self.instance.state, "done")
        self.assertFalse(self.instance.pause_requested)
        self.assertTrue(self.instance._resume.is_set())
