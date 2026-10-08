import threading
import time
import unittest
from unittest.mock import Mock, patch

from moneymin.web import server
from moneymin.web.catalog_loader import CatalogLoader


class CatalogLoadingTests(unittest.TestCase):
    def _finished_job(self, loader, key, result, status):
        started, accepted = loader.get(key, lambda _progress: (result, status),
                                       scope="campaign-tasks")
        self.assertEqual(accepted, 202)
        job_id = started["job_id"]
        for _ in range(100):
            if not loader.busy:
                return job_id
            time.sleep(.01)
        self.fail("catalog job did not finish")

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

    def test_timeout_identifies_live_worker_and_recovers_unobserved_result_after_ttl(self):
        loader = CatalogLoader(timeout_s=1)
        entered, release = threading.Event(), threading.Event()
        def slow(progress):
            entered.set()
            release.wait(5)
            return {"tasks": []}, 200
        work = Mock(side_effect=slow)
        try:
            initial, _ = loader.get((1,), work, scope="campaign-tasks")
            job_id = initial["job_id"]
            self.assertTrue(entered.wait(1))
            with patch("moneymin.web.catalog_loader.time.monotonic", return_value=time.monotonic() + 2):
                for _ in range(3):
                    body, status = loader.poll(job_id, key=(1,), scope="campaign-tasks")
                    self.assertEqual(status, 504)
                    self.assertTrue(body["loading"])
                    self.assertEqual(body["code"], "catalog_work_pending")
                    self.assertEqual(body["job_id"], job_id)
                    self.assertTrue(body["identity_bound"])
            self.assertEqual(work.call_count, 1)
        finally:
            release.set()
        for _ in range(100):
            with loader._lock:
                finished = "finished" in loader._jobs[(1,)]
            if finished:
                break
            time.sleep(.01)
        self.assertTrue(finished)
        with patch("moneymin.web.catalog_loader.time.monotonic", return_value=time.monotonic() + 600):
            self.assertEqual(loader.poll(job_id, key=(1,), scope="campaign-tasks"),
                             ({"tasks": []}, 200))
        self.assertEqual(work.call_count, 1)

    def test_poll_is_bound_to_current_account_identity_and_selection(self):
        loader = CatalogLoader()
        entered, release = threading.Event(), threading.Event()
        key = ("owner@example.invalid", "ambos", "both", 300, 1800, "identity-original")
        def slow(progress):
            progress("Contando anotações", phase="annotations")
            entered.set()
            release.wait(5)
            return {"tasks": [{"id": "private-result"}]}, 200
        work = Mock(side_effect=slow)
        try:
            initial, _ = loader.get(key, work, scope="campaign-tasks")
            self.assertTrue(entered.wait(1))
            for changed in (key[:-1] + ("revoked",), key[:3] + (60,) + key[4:],
                            ("different@example.invalid",) + key[1:]):
                body, status = loader.poll(initial["job_id"], key=changed, scope="campaign-tasks")
                self.assertEqual(status, 409)
                self.assertEqual(body["code"], "catalog_identity_changed")
                self.assertNotIn("tasks", body)
            self.assertEqual(loader.poll(initial["job_id"], key=key, scope="wrong")[1], 409)
            body, status = loader.poll(initial["job_id"], key=key, scope="campaign-tasks")
            self.assertEqual(status, 202)
            self.assertEqual(body["phase"], "annotations")
            self.assertEqual(work.call_count, 1)
        finally:
            release.set()

        for _ in range(100):
            with loader._lock:
                finished = "finished" in loader._jobs[key]
            if finished:
                break
            time.sleep(.01)
        self.assertTrue(finished)
        body, status = loader.poll(initial["job_id"], key=key[:-1] + ("revoked",),
                                   scope="campaign-tasks")
        self.assertEqual(status, 409)
        self.assertNotIn("tasks", body)
        self.assertEqual(loader.poll(initial["job_id"], key=key, scope="campaign-tasks"),
                         ({"tasks": [{"id": "private-result"}]}, 200))
        self.assertEqual(work.call_count, 1)

    def test_poll_missing_job_never_launches_work_and_cache_has_a_hard_bound(self):
        loader = CatalogLoader(ttl_s=600, max_cached=2)
        self.assertEqual(loader.poll("unknown", key=(0,))[1], 409)
        self.assertEqual(loader._jobs, {})
        identities = []
        for number in range(3):
            initial, _ = loader.get((number,), lambda progress: ({"tasks": []}, 200))
            identities.append(initial["job_id"])
            for _ in range(100):
                with loader._lock:
                    finished = "finished" in loader._jobs[(number,)]
                if finished:
                    break
                time.sleep(.01)
            self.assertTrue(finished)
        self.assertEqual(len(loader._jobs), 2)
        self.assertEqual(loader.poll(identities[0], key=(0,))[1], 409)
        self.assertEqual(loader.poll(identities[2], key=(2,)), ({"tasks": []}, 200))

    def test_consumed_result_expires_normally_without_poll_restarting_it(self):
        loader = CatalogLoader(ttl_s=1)
        work = Mock(return_value=({"tasks": []}, 200))
        initial, _ = loader.get((1,), work)
        for _ in range(100):
            result = loader.poll(initial["job_id"], key=(1,))
            if result[1] == 200:
                break
            time.sleep(.01)
        self.assertEqual(result, ({"tasks": []}, 200))
        with patch("moneymin.web.catalog_loader.time.monotonic", return_value=time.monotonic() + 2):
            body, status = loader.poll(initial["job_id"], key=(1,))
            self.assertEqual(status, 409)
            self.assertEqual(body["code"], "catalog_job_unavailable")
        self.assertEqual(work.call_count, 1)

    def test_account_exclusion_diagnostic_survives_unobserved_ttl_and_is_narrowly_bound(self):
        loader = CatalogLoader(ttl_s=.01)
        prefix = ("owner@example.invalid", "ego4d", "both", 300.0, 1800.0)
        key = (*prefix, "original-identity-fingerprint")
        issue = {"email": prefix[0], "code": "restricted",
                 "stage": "Carregamento remoto de categorias",
                 "reason": "Restrição confirmada.", "action": "Procure o suporte.",
                 "restriction_confirmed": True, "detail": "must-not-be-returned"}
        body = {"error": "private text that is reconstructed", "code": "catalog_account_unavailable",
                "issue": issue, "permanently_removed": True}
        job_id = self._finished_job(loader, key, body, 400)
        time.sleep(.03)  # The completed result remains until first delivery.

        diagnostic = loader.poll_account_exclusion_diagnostic(
            job_id, key_prefix=prefix, scope="campaign-tasks")
        self.assertEqual(diagnostic[1], 400)
        self.assertEqual(diagnostic[0]["code"], "catalog_account_unavailable")
        self.assertEqual(diagnostic[0]["issue"]["email"], prefix[0])
        self.assertNotIn("detail", diagnostic[0]["issue"])
        self.assertNotIn("tasks", diagnostic[0])
        self.assertNotIn("private text", diagnostic[0]["error"])
        self.assertEqual(loader.poll_account_exclusion_diagnostic(
            job_id, key_prefix=prefix, scope="campaign-tasks"), diagnostic)
        self.assertIsNone(loader.poll_account_exclusion_diagnostic(
            job_id, key_prefix=prefix, scope="different-scope"))
        self.assertIsNone(loader.poll_account_exclusion_diagnostic(
            job_id, key_prefix=(*prefix[:-1], 600.0), scope="campaign-tasks"))

    def test_account_exclusion_diagnostic_only_returns_pending_for_own_live_job(self):
        loader = CatalogLoader()
        prefix = ("owner@example.invalid", "ego4d", "both", 300.0, 1800.0)
        key = (*prefix, "original-identity-fingerprint")
        entered, release = threading.Event(), threading.Event()

        def work(progress):
            progress("private progress text")
            entered.set()
            self.assertTrue(release.wait(2))
            return {"tasks": [{"name": "must-not-be-returned"}]}, 200

        try:
            initial = loader.get(key, work, scope="campaign-tasks")[0]
            self.assertTrue(entered.wait(1))
            pending = loader.poll_account_exclusion_diagnostic(
                initial["job_id"], key_prefix=prefix, scope="campaign-tasks")
            self.assertEqual(pending[1], 202)
            self.assertEqual(pending[0]["job_id"], initial["job_id"])
            self.assertTrue(pending[0]["identity_bound"])
            self.assertTrue(pending[0]["loading"])
            self.assertNotIn("tasks", pending[0])
            self.assertNotIn("private progress", str(pending[0]))
            self.assertIsNone(loader.poll_account_exclusion_diagnostic(
                initial["job_id"], key_prefix=("other@example.invalid", *prefix[1:]),
                scope="campaign-tasks"))
            self.assertIsNone(loader.poll_account_exclusion_diagnostic(
                initial["job_id"], key_prefix=prefix, scope="other-scope"))
            self.assertIsNone(loader.poll_account_exclusion_diagnostic(
                initial["job_id"], key_prefix=(*prefix[:-1], 600.0),
                scope="campaign-tasks"))
        finally:
            release.set()

    def test_account_exclusion_diagnostic_rejects_success_and_unconfirmed_errors(self):
        prefix = ("owner@example.invalid", "ego4d", "both", 300.0, 1800.0)
        issue = {"email": prefix[0], "code": "restricted",
                 "stage": "Carregamento remoto de categorias",
                 "reason": "Restrição confirmada.", "action": "Procure o suporte.",
                 "restriction_confirmed": True}
        candidates = [
            ({"code": "catalog_account_unavailable", "issue": issue}, 400),
            ({"code": "catalog_account_unavailable", "issue": {**issue,
              "restriction_confirmed": False}, "permanently_removed": True}, 400),
            ({"code": "catalog_account_unavailable", "issue": {**issue,
              "stage": "Preparação local do catálogo"}, "permanently_removed": True}, 400),
            ({"code": "catalog_account_unavailable", "issue": issue,
              "permanently_removed": True, "tasks": []}, 400),
            ({"code": "catalog_account_unavailable", "issue": issue,
              "permanently_removed": True}, 200),
        ]
        for index, (result, status) in enumerate(candidates):
            with self.subTest(index=index):
                loader = CatalogLoader()
                key = (*prefix, f"identity-{index}")
                job_id = self._finished_job(loader, key, result, status)
                self.assertIsNone(loader.poll_account_exclusion_diagnostic(
                    job_id, key_prefix=prefix, scope="campaign-tasks"))

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
