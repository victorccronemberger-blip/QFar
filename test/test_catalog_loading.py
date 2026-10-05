import threading
import time
import unittest
from unittest.mock import Mock, patch

from moneymin.web import server
from moneymin.web.catalog_loader import CatalogLoader


class CatalogLoadingTests(unittest.TestCase):
    def test_pending_reads_return_immediately_and_share_one_build(self):
        loader = CatalogLoader()
        entered, release = threading.Event(), threading.Event()
        def slow(progress):
            progress("Indexing")
            entered.set()
            release.wait(5)
            return {"tasks": [{"id": "real-result"}]}, 200
        work = Mock(side_effect=slow)
        try:
            self.assertEqual(loader.get(("a",), work)[1], 202)
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            for _ in range(20):
                body, status = loader.get(("a",), work, refresh=True)
                self.assertEqual(status, 202)
                self.assertEqual(body["message"], "Indexing")
            self.assertLess(time.monotonic() - started, .5)
            self.assertEqual(work.call_count, 1)
        finally:
            release.set()
        for _ in range(100):
            result = loader.get(("a",), work)
            if result[1] == 200:
                break
            time.sleep(.01)
        self.assertEqual(result, ({"tasks": [{"id": "real-result"}]}, 200))
        self.assertEqual(work.call_count, 1)

    def test_manual_retry_replaces_cached_error_with_fresh_read(self):
        loader = CatalogLoader(ttl_s=600)
        work = Mock(side_effect=[({"error": "preserved"}, 409), ({"items": []}, 200)])
        def finished():
            for _ in range(100):
                result = loader.get(("recovery",), work)
                if result[1] != 202:
                    return result
                time.sleep(.01)
            self.fail("read did not finish")
        self.assertEqual(finished()[1], 409)
        self.assertEqual(loader.get(("recovery",), work)[1], 409)
        self.assertEqual(work.call_count, 1)
        self.assertEqual(loader.get(("recovery",), work, refresh=True)[1], 202)
        self.assertEqual(finished(), ({"items": []}, 200))
        self.assertEqual(work.call_count, 2)

    def test_selection_changes_do_not_create_unbounded_workers(self):
        loader = CatalogLoader(max_pending=1)
        release = threading.Event()
        try:
            loader.get(("old",), lambda progress: (release.wait(3), 200))
            work = Mock()
            for index in range(20):
                body, status = loader.get((index,), work)
                self.assertEqual((body["state"], status), ("queued", 202))
            work.assert_not_called()
        finally:
            release.set()

    def test_latest_duration_replaces_queued_catalog(self):
        loader = CatalogLoader()
        entered, release, latest_done = (threading.Event() for _ in range(3))
        def slow(progress):
            entered.set()
            release.wait(5)
            return {"tasks": []}, 200
        obsolete = Mock(return_value=({"tasks": []}, 200))
        def latest(progress):
            latest_done.set()
            return {"tasks": [{"id": "latest"}]}, 200
        try:
            loader.get((1,), slow, scope="campaign")
            self.assertTrue(entered.wait(1))
            loader.get((2,), obsolete, scope="campaign")
            loader.get((3,), latest, scope="campaign")
        finally:
            release.set()
        self.assertTrue(latest_done.wait(2))
        obsolete.assert_not_called()

    def test_timeout_stops_polling_without_starting_duplicate_work(self):
        loader = CatalogLoader(timeout_s=1)
        entered, release = threading.Event(), threading.Event()
        def slow(progress):
            entered.set()
            release.wait(5)
            return {"tasks": []}, 200
        work = Mock(side_effect=slow)
        try:
            loader.get((1,), work)
            self.assertTrue(entered.wait(1))
            with patch("moneymin.web.catalog_loader.time.monotonic", return_value=time.monotonic() + 2):
                for _ in range(3):
                    body, status = loader.get((1,), work)
                    self.assertEqual(status, 504)
                    self.assertFalse(body.get("loading"))
            self.assertEqual(work.call_count, 1)
        finally:
            release.set()

    def test_worker_exception_is_terminal_and_does_not_leak_secrets(self):
        loader = CatalogLoader()
        work = Mock(side_effect=RuntimeError("private-token"))
        for _ in range(100):
            result = loader.get(("error",), work)
            if result[1] != 202:
                break
            time.sleep(.01)
        self.assertEqual(result[1], 500)
        self.assertNotIn("private-token", str(result))
        self.assertEqual(work.call_count, 1)

    def test_async_endpoint_does_not_hold_http_or_account_lock_while_indexing(self):
        app = server.create_app(for_testing=True)
        entered, release = threading.Event(), threading.Event()
        def build(*args, **kwargs):
            self.assertEqual(kwargs["remote_tasks"], [{"name": "Gardening"}])
            entered.set()
            release.wait(5)
            return [{"id": "garden", "available_for_duration": True}]
        session = Mock()
        session.all_tasks.return_value = [{"name": "Gardening"}]
        with patch.object(server.Session, "from_email", return_value=session), \
             patch.object(server, "_resolve_org", return_value="org"), \
             patch.object(server.campaign, "available_tasks", side_effect=build) as ranking:
            client = app.test_client()
            path = "/api/tasks?async=1&email=a@example.com&dataset=ego4d&min_dur_s=300&max_dur_s=1800"
            try:
                self.assertEqual(client.get(path).status_code, 202)
                self.assertTrue(entered.wait(1))
                for _ in range(5):
                    self.assertEqual(client.get(path).status_code, 202)
                self.assertEqual(client.get("/api/health").status_code, 200)
                acquired = server._ACCOUNT_OPERATION_LOCK.acquire(blocking=False)
                self.assertTrue(acquired)
                if acquired:
                    server._ACCOUNT_OPERATION_LOCK.release()
            finally:
                release.set()
            for _ in range(100):
                response = client.get(path)
                if response.status_code != 202:
                    break
                time.sleep(.01)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["tasks"][0]["id"], "garden")
            self.assertEqual(ranking.call_count, 1)

    def test_heavy_read_screens_complete_without_blocking_http(self):
        from moneymin import recovery
        for path, target, attribute, payload in (
            ("/api/recovery?async=1", recovery, "snapshot", {"items": [], "pending": 0}),
            ("/api/holo-cache?async=1&provider=ego4d", server.ego_accelerator,
             "cache_status", {"total": 7, "ready": 2}),
        ):
            with self.subTest(path=path):
                entered, release = threading.Event(), threading.Event()
                def slow(*args, **kwargs):
                    entered.set()
                    release.wait(3)
                    return payload
                with patch.object(target, attribute, side_effect=slow) as work:
                    client = server.create_app(for_testing=True).test_client()
                    try:
                        self.assertEqual(client.get(path).status_code, 202)
                        self.assertTrue(entered.wait(1))
                        self.assertEqual(client.get(path).status_code, 202)
                        self.assertEqual(client.get("/api/health").status_code, 200)
                    finally:
                        release.set()
                    for _ in range(100):
                        response = client.get(path)
                        if response.status_code != 202:
                            break
                        time.sleep(.01)
                    self.assertEqual(response.status_code, 200)
                    self.assertFalse(response.json.get("loading"))
                    self.assertEqual(work.call_count, 1)


if __name__ == "__main__":
    unittest.main()
