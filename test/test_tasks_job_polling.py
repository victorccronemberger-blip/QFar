"""A category job survives timeouts while retaining its selected account binding."""
from contextlib import ExitStack
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from moneymin import config
from moneymin.web import server
from moneymin.web.catalog_loader import CatalogLoader


class TasksJobPollingTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name, value in (("DATA_DIR", self.root / "state"),
                            ("MEDIA_DATA_DIR", self.root / "library"),
                            ("SECRETS_DIR", self.root / "fixture-access")):
            value.mkdir()
            self.stack.enter_context(patch.object(config, name, value))
        self.stack.enter_context(patch.dict(os.environ, {"QMONEY_LOCAL_API_TOKEN": ""}))
        self.removed = set()
        self.stack.enter_context(patch.object(server, "_removed_accounts",
                                             side_effect=lambda: self.removed.copy()))
        self.stack.enter_context(patch.object(server, "ORG_MIGRATION", Mock(running=False)))
        self.stack.enter_context(patch.dict(server._BULK_REGISTER_STATE, {"state": "idle"}))
        self.token = {"email": "fixture@example.invalid", "localId": "fixture-subject",
                      "idToken": "fixture-id", "refreshToken": "fixture-refresh",
                      "organization_id": "fixture-organization", "tenantId": "fixture-tenant"}
        self.token_path = config.token_path(self.token["email"])
        self.write_token()
        self.clock = 100.0
        self.stack.enter_context(patch("moneymin.web.catalog_loader.time.monotonic",
                                       side_effect=lambda: self.clock))
        self.loaders = []
        self.stack.enter_context(patch.object(server, "CatalogLoader", side_effect=self.loader))
        self.entered, self.release = threading.Event(), threading.Event()
        self.session = Mock()
        self.session.all_tasks.return_value = [{"id": "fixture-task", "name": "Gardening"}]
        self.sessions = self.stack.enter_context(patch.object(server.Session, "from_email",
                                                             return_value=self.session))
        self.resolve = self.stack.enter_context(patch.object(server, "_resolve_org",
                                                            side_effect=self.resolve_org))
        self.catalog = self.stack.enter_context(patch.object(server.campaign, "available_tasks",
                                                            side_effect=self.build_catalog))
        self.client = server.create_app(for_testing=True).test_client()
        self.query = {"async": "1", "email": self.token["email"], "dataset": "ego4d",
                      "content_mode": "both", "min_dur_s": 300, "max_dur_s": 1800}
        self.addCleanup(self.finish_worker)

    def loader(self, **kwargs):
        instance = CatalogLoader(**kwargs)
        self.loaders.append(instance)
        return instance

    def write_token(self):
        self.token_path.write_text(json.dumps(self.token), encoding="utf-8")

    def resolve_org(self, _email, **_kwargs):
        # Organization resolution writes this derived cache during the job.
        self.token["org_key"] = "fixture-resolved-organization"
        self.write_token()
        return self.token["org_key"]

    def build_catalog(self, _email, _org, **kwargs):
        self.assertEqual(kwargs["remote_tasks"], self.session.all_tasks.return_value)
        self.entered.set()
        if not self.release.wait(5):
            raise AssertionError("fixture category worker was not released")
        return [{"id": "fixture-task", "name": "Gardening", "clip_count": 2,
                 "available_for_duration": True, "requires_measured_validation": True}]

    def finish_worker(self):
        self.release.set()
        for _ in range(200):
            if not self.loaders or not self.loaders[0].busy:
                return
            time.sleep(.005)
        self.fail("fixture category worker did not finish")

    def start_job(self):
        response = self.client.get("/api/tasks", query_string=self.query)
        self.assertEqual(response.status_code, 202, response.get_json())
        self.assertTrue(self.entered.wait(1))
        body = response.get_json()
        self.assertTrue(body["loading"])
        self.assertTrue(body["identity_bound"])
        return body["job_id"]

    def poll(self, job_id, **changes):
        return self.client.get("/api/tasks", query_string={**self.query, "job_id": job_id, **changes})

    def assert_one_build(self):
        self.sessions.assert_called_once_with(self.query["email"])
        self.resolve.assert_called_once()
        self.session.all_tasks.assert_called_once_with("fixture-resolved-organization")
        self.catalog.assert_called_once()

    def test_timeout_keeps_live_job_and_poll_returns_unobserved_completion(self):
        job = self.start_job()
        self.clock += 301
        preparing = self.poll(job)
        self.assertEqual(preparing.status_code, 202, preparing.get_json())
        self.assertEqual(preparing.json['job_id'], job)
        self.clock += 1500
        for _ in range(3):
            response = self.poll(job)
            self.assertEqual(response.status_code, 504, response.get_json())
            self.assertTrue(response.json["loading"])
            self.assertEqual(response.json["job_id"], job)
            self.assertEqual(response.json["code"], "catalog_work_pending")
        self.assertTrue(self.loaders[0].busy)
        repeated = self.client.get("/api/tasks", query_string={**self.query, "refresh": "1"})
        self.assertEqual(repeated.status_code, 504)
        self.assertEqual(repeated.json["job_id"], job)
        self.assert_one_build()
        self.finish_worker()
        # The terminal result has not yet been observed, even beyond normal TTL.
        self.clock += 600
        completed = self.poll(job)
        self.assertEqual(completed.status_code, 200, completed.get_json())
        self.assertEqual(completed.json["tasks"][0]["clip_count"], 2)
        self.assertFalse(completed.json.get("loading", False))
        self.assert_one_build()

    def test_rotation_and_cached_org_changes_reuse_same_job_and_result(self):
        job = self.start_job()
        initial_fingerprint = server._catalog_identity_fingerprint([self.query["email"]])
        original_preflight = server._preflight_fingerprint([self.query["email"]])
        self.token.update(idToken="fixture-rotated-id", refreshToken="fixture-rotated-refresh",
                          org_key="another-derived-cache")
        self.write_token()
        self.assertEqual(server._catalog_identity_fingerprint([self.query["email"]]),
                         initial_fingerprint)
        self.assertNotEqual(server._preflight_fingerprint([self.query["email"]]),
                            original_preflight)
        pending = self.poll(job)
        self.assertEqual(pending.status_code, 202, pending.get_json())
        self.assertEqual(pending.json["job_id"], job)
        self.finish_worker()
        completed = self.poll(job)
        self.assertEqual(completed.status_code, 200, completed.get_json())
        self.assertEqual(completed.json["org_key"], "fixture-resolved-organization")
        self.assert_one_build()

    def test_changed_selection_rejects_job_without_starting_new_work(self):
        job = self.start_job()
        for changes in ({"email": "other@example.invalid"}, {"dataset": "nymeria"},
                        {"content_mode": "cache"}, {"min_dur_s": 600}, {"max_dur_s": 900}):
            with self.subTest(changes=changes):
                response = self.poll(job, **changes)
                self.assertEqual(response.status_code, 409, response.get_json())
                self.assertEqual(response.json["code"], "catalog_identity_changed")
                self.assertNotIn("tasks", response.json)
        self.assert_one_build()
        self.assertEqual(self.poll(job).status_code, 202)

    def test_changed_identity_organization_or_credential_presence_rejects_completed_result(self):
        job = self.start_job()
        self.finish_worker()
        unchanged = copy.deepcopy(self.token)
        for changes in ({"localId": "different-subject"}, {"organization_id": "different-org"},
                        {"tenantId": "different-tenant"}, {"idToken": None}, {"refreshToken": None}):
            with self.subTest(changes=changes):
                self.token = copy.deepcopy(unchanged)
                for key, value in changes.items():
                    if value is None:
                        self.token.pop(key)
                    else:
                        self.token[key] = value
                self.write_token()
                response = self.poll(job)
                self.assertEqual(response.status_code, 409, response.get_json())
                self.assertEqual(response.json["code"], "catalog_identity_changed")
                self.assertNotIn("tasks", response.json)
        self.token = unchanged
        self.write_token()
        self.assertEqual(self.poll(job).status_code, 200)
        self.assert_one_build()

    def test_removed_credentials_and_revocation_cannot_claim_old_result(self):
        job = self.start_job()
        self.finish_worker()
        self.token_path.unlink()
        absent = self.poll(job)
        self.assertEqual(absent.status_code, 409, absent.get_json())
        self.assertNotIn("tasks", absent.json)
        self.write_token()
        self.removed.add(self.query["email"])
        revoked = self.poll(job)
        self.assertEqual(revoked.status_code, 409, revoked.get_json())
        self.assertNotIn("tasks", revoked.json)
        self.removed.clear()
        self.assertEqual(self.poll(job).status_code, 200)
        self.assert_one_build()

    def test_unknown_job_never_creates_session_or_catalog(self):
        for job in ("unknown", "a" * 32):
            response = self.poll(job)
            self.assertEqual(response.status_code, 409, response.get_json())
            self.assertEqual(response.json["code"], "catalog_job_unavailable")
            self.assertNotIn("tasks", response.json)
        self.sessions.assert_not_called()
        self.resolve.assert_not_called()
        self.session.all_tasks.assert_not_called()
        self.catalog.assert_not_called()

    def test_only_explicit_refresh_without_job_retries_terminal_failure(self):
        self.catalog.side_effect = [RuntimeError("fixture-private-error"),
                                   [{"id": "fixture-recovered-task", "clip_count": 1}]]
        initial = self.client.get("/api/tasks", query_string=self.query)
        self.assertEqual(initial.status_code, 202, initial.get_json())
        first_job = initial.json["job_id"]
        self.finish_worker()
        failed = self.poll(first_job, refresh="1")
        self.assertEqual(failed.status_code, 400, failed.get_json())
        self.assertNotIn("fixture-private-error", failed.get_data(as_text=True))
        self.assertEqual(self.client.get("/api/tasks", query_string=self.query).status_code, 400)
        self.assertEqual(self.sessions.call_count, 1)
        self.assertEqual(self.catalog.call_count, 1)
        retried = self.client.get("/api/tasks", query_string={**self.query, "refresh": "1"})
        self.assertEqual(retried.status_code, 202, retried.get_json())
        self.assertNotEqual(retried.json["job_id"], first_job)
        self.finish_worker()
        completed = self.poll(retried.json["job_id"])
        self.assertEqual(completed.status_code, 200, completed.get_json())
        self.assertEqual(completed.json["tasks"][0]["id"], "fixture-recovered-task")
        self.assertEqual(self.sessions.call_count, 2)
        self.assertEqual(self.resolve.call_count, 2)
        self.assertEqual(self.session.all_tasks.call_count, 2)
        self.assertEqual(self.catalog.call_count, 2)


if __name__ == "__main__":
    unittest.main()
