"""Library jobs use isolated source data and cannot authenticate to Minute."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import ANY, Mock, patch

from moneymin import config, nymeria_library, recovery
from moneymin.web import server
from moneymin.web.library_runner import LibraryPreparationRunner
from moneymin.web.runner import CampaignRunner


class LibraryPreparationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="qmoney-library-api-")))
        self.stack.enter_context(patch.object(config, "DATA_DIR", root))
        self.stack.enter_context(patch.object(config, "MEDIA_DATA_DIR", root / "media"))
        self.worker = LibraryPreparationRunner()
        self.addCleanup(self.finish)

    def finish(self):
        self.worker.stop()
        if self.worker._thread:
            self.worker._thread.join(3)

    def wait_done(self):
        self.worker._thread.join(3)
        self.assertFalse(self.worker.running)
        return self.worker.snapshot()

    def test_stop_preserves_result_and_parallel_start_is_rejected(self):
        entered = threading.Event()
        def work(progress, stopped):
            progress({"message": "Measured source", "completed": 1, "total": 2,
                      "download_url": "https://private.fbcdn.net/?signature=inert"})
            entered.set()
            while not stopped():
                time.sleep(.01)
            return {"verified_sources": 1}
        self.worker.start("download", work, root="isolated")
        self.assertTrue(entered.wait(1))
        with self.assertRaises(RuntimeError):
            self.worker.start("sync", work, root="isolated")
        self.assertNotIn("download_url", self.worker.snapshot())
        self.worker.stop()
        result = self.wait_done()
        self.assertEqual(result["state"], "stopped")
        self.assertEqual(result["result"], {"verified_sources": 1})

    def test_provider_error_does_not_disclose_signed_urls(self):
        def work(progress, stopped):
            raise RuntimeError("https://private.fbcdn.net/?signature=inert-secret")
        self.worker.start("download", work, root="isolated")
        result = self.wait_done()
        self.assertEqual(result["state"], "error")
        self.assertNotIn("inert-secret", str(result))
        self.assertIsNone(result["result"])

    def test_terminal_job_can_start_another_generation(self):
        first = self.worker.start("sync", lambda progress, stopped: {"sequences": 1100}, root="isolated")
        self.assertEqual(self.wait_done()["state"], "done")
        second = self.worker.start("download", lambda progress, stopped: {"measured": 1}, root="isolated")
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(self.wait_done()["result"], {"measured": 1})

    def test_known_local_checker_failure_reports_its_safe_message_and_code(self):
        def work(progress, stopped):
            progress({"message": "Verificando fonte", "sequence_id": "inert-sequence"})
            raise nymeria_library.NymeriaLibraryError("Sequência inert-sequence: relógio inválido.",
                                                     "source_timestamps_invalid")
        self.worker.start("download", work, root="isolated")
        result = self.wait_done()
        self.assertEqual(result["state"], "error")
        self.assertEqual(result["error_code"], "source_timestamps_invalid")
        self.assertEqual(result["sequence_id"], "inert-sequence")
        self.assertEqual(result["message"], "Sequência inert-sequence: relógio inválido.")


class NymeriaLibraryApiTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="qmoney-nymeria-api-")))
        for name, value in (("DATA_DIR", self.root / "data"),
                            ("MEDIA_DATA_DIR", self.root / "media"),
                            ("SECRETS_DIR", self.root / "secrets")):
            value.mkdir(parents=True)
            self.stack.enter_context(patch.object(config, name, value))
        self.stack.enter_context(patch.object(server, "RUNNER", CampaignRunner()))
        self.stack.enter_context(patch.object(server, "RECOVERY", recovery.RecoveryRunner()))
        self.stack.enter_context(patch.object(server, "HOLO_CACHE_RUNNER", Mock(running=False, _thread=None)))
        self.auth = self.stack.enter_context(patch.object(server.Session, "from_email",
                                                        side_effect=AssertionError("Minute forbidden")))
        self.app = server.create_app(for_testing=True)
        self.client = self.app.test_client()
        self.worker = self.app.extensions["nymeria_library_worker"]
        self.addCleanup(self.finish)

    def finish(self):
        self.worker.stop()
        if self.worker._thread:
            self.worker._thread.join(3)
        self.auth.assert_not_called()

    def wait_worker(self):
        self.worker._thread.join(3)
        self.assertFalse(self.worker.running)
        return self.worker.snapshot()

    def test_inventory_exposes_tasks_and_source_summary_without_network(self):
        result = self.client.get("/api/library/nymeria/sequences").get_json()
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["summary"]["sequence_count"], 0)
        self.assertIn("Shopping", result["task_names"])
        self.assertEqual(len(result["task_names"]), 49)
        self.assertNotIn("Gardening", result["task_names"])
        self.assertEqual(result["task_catalog_source"], "local_snapshot_requires_campaign_preflight")
        self.assertEqual(result["worker"]["state"], "idle")

    def test_invalid_requests_never_start_a_source_worker(self):
        for path, body in (("import", {}), ("sync", {"unexpected": 1}),
                           ("download", {"seq_ids": []}),
                           ("download", {"seq_ids": ["fixture"], "min_free_gb": True}),
                           ("plan", {"task_names": []}),
                           ("plan", {"task_names": ["Shopping"], "target_seconds": float("inf")}),
                           ("plan", {"task_names": ["Shopping"], "target_seconds": True}),
                           ("plan", {"task_names": ["Shopping"], "min_free_gb": 4})):
            with self.subTest(path=path, body=body):
                self.assertEqual(self.client.post("/api/library/nymeria/" + path, json=body).status_code, 400)
                self.assertFalse(self.worker.running)

    def test_download_binds_ids_and_disk_reserve_and_blocks_reset_until_stopped(self):
        entered = threading.Event()
        def acquire(ids, progress, should_stop, min_free_bytes):
            self.assertEqual(ids, ["source-fixture"])
            self.assertEqual(min_free_bytes, 60 * 1024**3)
            entered.set()
            while not should_stop():
                time.sleep(.01)
            return {"ok": True, "verified_sources": 1}
        with patch.object(nymeria_library, "acquire_sequences", side_effect=acquire):
            response = self.client.post("/api/library/nymeria/download",
                                        json={"seq_ids": ["source-fixture"], "min_free_gb": 60})
            self.assertEqual(response.status_code, 202)
            self.assertTrue(entered.wait(1))
            blocked = self.client.post("/api/campaign/reset", json={})
            self.assertEqual(blocked.status_code, 409)
            self.assertEqual(blocked.get_json()["error_code"], "campaign_reset_busy")
            self.assertEqual(self.client.post("/api/library/nymeria/sync", json={}).status_code, 409)
            for path in ("/api/campaigns", "/api/campaigns/original", "/api/campaigns/preflight",
                         "/api/campaigns/original/preflight", "/api/recovery/resume"):
                with self.subTest(path=path):
                    admission = self.client.post(path, json={})
                    self.assertEqual(admission.status_code, 409)
                    self.assertEqual(admission.get_json()["error_code"], "library_preparation_busy")
            self.assertEqual(self.client.post("/api/library/nymeria/plan",
                json={"task_names": ["Shopping"]}).status_code, 409)
            self.assertEqual(self.client.post("/api/storage/cleanup", json={}).status_code, 409)
            self.assertTrue(self.client.post("/api/library/nymeria/stop", json={}).get_json()["ok"])
            self.assertEqual(self.wait_worker()["state"], "stopped")

    def test_close_stops_library_job_and_drain_waits_for_its_actual_exit(self):
        entered, release = threading.Event(), threading.Event()
        def sync(progress, should_stop):
            entered.set()
            release.wait(2)
            return {"stopped": should_stop()}
        with patch.object(nymeria_library, "sync_catalog", side_effect=sync):
            self.assertEqual(self.client.post("/api/library/nymeria/sync", json={}).status_code, 202)
            self.assertTrue(entered.wait(1))
            draining = self.client.post("/api/campaigns/drain", json={}).get_json()
            self.assertFalse(draining["ready"])
            release.set()
            self.wait_worker()
            self.assertTrue(self.client.post("/api/campaigns/drain", json={}).get_json()["ready"])
            self.assertEqual(self.client.post("/api/library/nymeria/sync", json={}).status_code, 409)

    def test_plan_is_async_and_never_invents_measured_capacity(self):
        expected = {"campaign_ready": False, "target_found_in_annotations": True,
                    "selected_seq_ids": ["real-catalog-fixture"], "selected_potential_seconds": 28800}
        with patch.object(nymeria_library, "plan_expansion", return_value=expected) as plan:
            body = {"task_names": ["Shopping"], "target_seconds": 28800, "min_free_gb": 60,
                    "min_dur_s": 300, "max_dur_s": 1800}
            for _ in range(100):
                response = self.client.post("/api/library/nymeria/plan", json=body)
                if response.status_code != 202:
                    break
                time.sleep(.01)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), expected)
            plan.assert_called_once_with(["Shopping"], min_dur_s=300, max_dur_s=1800,
                                         target_seconds=28800, progress=ANY, min_free_bytes=60 * 1024**3)

    def test_catalog_update_waits_for_a_running_plan(self):
        entered, release = threading.Event(), threading.Event()
        body = {"task_names": ["Shopping"]}
        def plan(*args, **kwargs):
            entered.set()
            release.wait(3)
            return {"campaign_ready": False}
        with patch.object(nymeria_library, "plan_expansion", side_effect=plan):
            try:
                self.assertEqual(self.client.post("/api/library/nymeria/plan", json=body).status_code, 202)
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.client.post("/api/library/nymeria/sync", json={}).status_code, 409)
                self.assertFalse(self.worker.running)
            finally:
                release.set()
            for _ in range(100):
                result = self.client.post("/api/library/nymeria/plan", json=body)
                if result.status_code != 202:
                    break
                time.sleep(.01)
            self.assertEqual(result.status_code, 200)
