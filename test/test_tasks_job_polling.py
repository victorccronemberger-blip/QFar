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

from moneymin import config, secure_store, token_store
from moneymin.minute_api import AuthError
from moneymin.web import server
from moneymin.web.catalog_loader import CatalogLoader


class TasksJobPollingTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.install, self.library = self.root / "installation", self.root / "library"
        for name, value in (("ROOT", self.install), ("LIBRARY_ROOT", self.library),
                            ("DATA_DIR", self.install / "data"),
                            ("MEDIA_DATA_DIR", self.library / "data"),
                            ("SECRETS_DIR", self.install / "secrets")):
            value.mkdir(parents=True)
            self.stack.enter_context(patch.object(config, name, value))
        for name, value in (("PREFS_PATH", config.DATA_DIR / "webui_prefs.json"),
                            ("BALANCES_PATH", config.DATA_DIR / "balances.json"),
                            ("CROWTADO_PW_PATH", config.SECRETS_DIR / "crowtado_passwords.json"),
                            ("ACCOUNT_HEALTH_PATH", config.DATA_DIR / "account_health.json")):
            self.stack.enter_context(patch.object(server, name, value))
        self.stack.enter_context(patch.object(secure_store, "protect_json",
                                             side_effect=lambda value: json.dumps(value).encode()))
        self.stack.enter_context(patch.object(secure_store, "unprotect_json",
                                             side_effect=lambda payload: json.loads(payload)))
        self.stack.enter_context(patch.dict(os.environ, {"QMONEY_LOCAL_API_TOKEN": ""}))
        self.removed = set()
        self.actual_removed_accounts = server._removed_accounts
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

    def test_remote_restriction_is_a_safe_account_failure_and_archives_only_after_success(self):
        self.session.all_tasks.side_effect = AuthError(
            "restricted account response with private details", code="restricted")
        archived = []

        def archive(email, result):
            archived.append((email, copy.deepcopy(result)))
            result["permanently_removed"] = True

        with patch.object(server, "_archive_checked_ban", side_effect=archive):
            started = self.client.get("/api/tasks", query_string=self.query)
            self.assertEqual(started.status_code, 202, started.get_json())
            self.finish_worker()
            failed = self.poll(started.json["job_id"])

        self.assertEqual(failed.status_code, 400, failed.get_json())
        self.assertEqual(failed.json["code"], "catalog_account_unavailable")
        self.assertEqual(failed.json["issue"]["email"], self.query["email"])
        self.assertEqual(failed.json["issue"]["code"], "restricted")
        self.assertTrue(failed.json["issue"]["restriction_confirmed"])
        self.assertTrue(failed.json["permanently_removed"])
        self.assertNotIn("private details", failed.get_data(as_text=True))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0][0], self.query["email"])
        self.assertEqual(archived[0][1]["status"], "disabled")
        self.assertEqual(self.catalog.call_count, 0)

    def test_confirmed_restriction_is_not_claimed_removed_if_archive_fails(self):
        self.sessions.side_effect = AuthError("restricted", code="restricted")
        with patch.object(server, "_archive_checked_ban",
                          side_effect=OSError("fixture-private archive path")) as archive:
            started = self.client.get("/api/tasks", query_string=self.query)
            self.assertEqual(started.status_code, 202, started.get_json())
            self.finish_worker()
            failed = self.poll(started.json["job_id"])

        self.assertEqual(failed.status_code, 400, failed.get_json())
        self.assertEqual(failed.json["code"], "catalog_account_unavailable")
        self.assertTrue(failed.json["issue"]["restriction_confirmed"])
        self.assertNotIn("permanently_removed", failed.json)
        self.assertTrue(failed.json["removal_failed"])
        self.assertEqual(failed.json["archive_issue"]["code"], "local_archive_failed")
        self.assertNotIn("fixture-private", failed.get_data(as_text=True))
        archive.assert_called_once()
        self.catalog.assert_not_called()

    def test_transient_remote_failure_does_not_archive_or_claim_permanent_removal(self):
        self.sessions.side_effect = AuthError("fixture private network detail", code="network")
        with patch.object(server, "_archive_checked_ban") as archive:
            started = self.client.get("/api/tasks", query_string=self.query)
            self.assertEqual(started.status_code, 202, started.get_json())
            self.finish_worker()
            failed = self.poll(started.json["job_id"])

        self.assertEqual(failed.status_code, 400, failed.get_json())
        self.assertEqual(failed.json["code"], "catalog_account_unavailable")
        self.assertEqual(failed.json["issue"]["code"], "network")
        self.assertNotIn("permanently_removed", failed.json)
        self.assertNotIn("fixture private network detail", failed.get_data(as_text=True))
        archive.assert_not_called()
        self.catalog.assert_not_called()

    def test_local_catalog_failure_is_not_misreported_as_account_unavailable(self):
        def fail_local(*_args, **_kwargs):
            self.entered.set()
            if not self.release.wait(5):
                raise AssertionError("fixture local catalog worker was not released")
            raise AuthError("restricted local fixture", code="restricted")

        self.catalog.side_effect = fail_local
        with patch.object(server, "_archive_checked_ban") as archive:
            job = self.start_job()
            self.finish_worker()
            failed = self.poll(job)

        self.assertEqual(failed.status_code, 400, failed.get_json())
        self.assertNotEqual(failed.json.get("code"), "catalog_account_unavailable")
        self.assertNotIn("permanently_removed", failed.json)
        archive.assert_not_called()

    def test_synchronous_remote_restriction_uses_same_safe_account_contract(self):
        self.sessions.side_effect = AuthError("restricted", code="restricted")
        query = {key: value for key, value in self.query.items() if key != "async"}

        def archive(_email, result):
            result["permanently_removed"] = True

        with patch.object(server, "_archive_checked_ban", side_effect=archive):
            failed = self.client.get("/api/tasks", query_string=query)

        self.assertEqual(failed.status_code, 400, failed.get_json())
        self.assertEqual(failed.json["code"], "catalog_account_unavailable")
        self.assertTrue(failed.json["issue"]["restriction_confirmed"])
        self.assertTrue(failed.json["permanently_removed"])
        self.catalog.assert_not_called()

    def test_synchronous_local_failure_keeps_catalog_error_contract(self):
        self.catalog.side_effect = AuthError("restricted local fixture", code="restricted")
        query = {key: value for key, value in self.query.items() if key != "async"}
        with patch.object(server, "_archive_checked_ban") as archive:
            failed = self.client.get("/api/tasks", query_string=query)

        self.assertEqual(failed.status_code, 400, failed.get_json())
        self.assertEqual(failed.json["code"], "task_catalog_unavailable")
        self.assertNotIn("permanently_removed", failed.json)
        archive.assert_not_called()

    def test_real_ban_allows_only_own_terminal_exclusion_poll(self):
        self.sessions.side_effect = AuthError("restricted", code="restricted")
        query = dict(self.query)
        with patch.object(server, "_removed_accounts", self.actual_removed_accounts):
            started = self.client.get("/api/tasks", query_string=query)
            self.assertEqual(started.status_code, 202, started.get_json())
            job_id = started.json["job_id"]
            self.finish_worker()

            # The ban mechanism writes the canonical archive and removes the actual
            # token file; no mocked permanence flag is involved in this recovery.
            self.assertIsNone(token_store.load(config.SECRETS_DIR, self.token["email"], migrate=False))
            archive = server.banned_store.load(config.DATA_DIR / "banned_accounts.json")
            record = next(row for row in archive["accounts"]
                          if row["email"] == self.token["email"])
            self.assertIs(record["restriction_confirmed"], True)
            self.assertEqual(record["stage"], "Carregamento remoto de categorias")

            failed = self.poll(job_id)
            self.assertEqual(failed.status_code, 400, failed.get_json())
            self.assertEqual(failed.json["code"], "catalog_account_unavailable")
            self.assertEqual(failed.json["issue"]["email"], self.token["email"])
            self.assertIs(failed.json["issue"]["restriction_confirmed"], True)
            self.assertIs(failed.json["permanently_removed"], True)
            self.assertNotIn("tasks", failed.json)
            repeated = self.poll(job_id)
            self.assertEqual(repeated.status_code, 400, repeated.get_json())
            self.assertEqual(repeated.json, failed.json)

            other_email = "other-owner@example.invalid"
            token_store.save(config.SECRETS_DIR, other_email, {
                "email": other_email, "localId": "other-subject", "idToken": "other-id",
                "refreshToken": "other-refresh"})
            other = self.client.get("/api/tasks", query_string={
                **query, "email": other_email, "job_id": job_id})
            self.assertEqual(other.status_code, 409, other.get_json())

            changed_mode = self.poll(job_id, content_mode="cache")
            self.assertEqual(changed_mode.status_code, 409, changed_mode.get_json())
            changed_duration = self.poll(job_id, min_dur_s=600)
            self.assertEqual(changed_duration.status_code, 409, changed_duration.get_json())

            # Even a replacement token for the same stable subject must fail closed.
            token_store.save(config.SECRETS_DIR, self.token["email"], {
                "email": self.token["email"], "localId": self.token["localId"],
                "idToken": "replacement-id", "refreshToken": "replacement-refresh"})
            replaced = self.poll(job_id)
            self.assertEqual(replaced.status_code, 409, replaced.get_json())


if __name__ == "__main__":
    unittest.main()
