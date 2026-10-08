"""Slow account checks must not hold a desktop HTTP request open."""
import threading
import time
import unittest
import uuid
from contextlib import ExitStack
from unittest.mock import Mock, patch

from moneymin.minute_api import AuthError
from moneymin.web import server


class AsyncPreflightTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.entered, self.release, self.finished = (threading.Event() for _ in range(3))
        self.real_resolve = server._resolve_org
        self.runner = self.stack.enter_context(patch.object(server, "RUNNER", Mock(running=False)))
        self.stack.enter_context(patch.object(server, "HOLO_CACHE_RUNNER", Mock(running=False)))
        self.stack.enter_context(patch.object(server, "_list_accounts", return_value=[{"email": "fixture@example.invalid"}]))
        self.stack.enter_context(patch.object(server, "_preflight_fingerprint", return_value="fixture-owner"))
        self.stack.enter_context(patch.object(server.recovery, "snapshot", return_value={"items": []}))
        self.stack.enter_context(patch.object(server.campaign, "available_tasks", return_value=[
            {"id": "task", "name": "Task", "scenario": "task", "clip_count": 1, "available_for_duration": True}]))
        self.stack.enter_context(patch.object(server.readiness, "campaign_readiness", return_value={"ready": True, "checks": []}))
        self.stack.enter_context(patch.object(server, "_storage_snapshot", return_value={"free_bytes": 20 * 1024**3}))
        self._resolve_patch = patch.object(server, "_resolve_org", side_effect=self.slow_access)
        self.resolve = self.stack.enter_context(self._resolve_patch)
        self.client = server.create_app(for_testing=True).test_client()
        self.body = {"accounts": ["fixture@example.invalid"], "tasks": [{"task_id": "task"}], "dataset": "ego4d"}
        self.url = "/api/campaigns/preflight?async=1&request_id=" + "a" * 32
        self.addCleanup(self.unblock)

    def slow_access(self, email):
        self.entered.set()
        if not self.release.wait(5):
            raise AssertionError("fixture was not released")
        return "org"

    def unblock(self):
        self.release.set()
        if self.entered.is_set():
            self.finished.wait(2)

    def finish(self):
        self.release.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.client.post(self.url, json=self.body)
            if result.status_code != 202:
                self.finished.set()
                return result
            time.sleep(.01)
        self.fail("background verification did not finish")

    def test_slow_access_returns_progress_and_reuses_one_verification(self):
        first = self.client.post(self.url, json=self.body)
        self.assertEqual(first.status_code, 202)
        self.assertTrue(self.entered.wait(1))
        started = time.monotonic()
        for _ in range(20):
            response = self.client.post(self.url, json=self.body)
            self.assertEqual(response.status_code, 202)
            self.assertTrue(response.json["loading"])
            self.assertIn("acesso", response.json["message"])
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(self.resolve.call_count, 1)
        response = self.finish()
        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(response.json["ok"])
        self.assertTrue(response.json["preflight_id"])
        repeated = self.client.post(self.url, json=self.body)
        self.assertEqual(repeated.json["preflight_id"], response.json["preflight_id"])
        self.assertEqual(self.resolve.call_count, 1)
        self.runner.start.assert_not_called()

    def test_account_failure_remains_actionable_and_never_starts_campaign(self):
        self.resolve.side_effect = AuthError("private-token", code="authentication")
        response = self.finish()
        self.assertEqual(response.status_code, 200, response.json)
        self.assertFalse(response.json["ok"])
        self.assertIsNone(response.json["preflight_id"])
        self.assertEqual(response.json["account_issues"][0]["code"], "authentication")
        self.assertNotIn("private-token", response.get_data(as_text=True))
        self.runner.start.assert_not_called()

    def test_preflight_org_validation_uses_assigned_identity_route(self):
        from moneymin import registration_proxy

        self.url = "/api/campaigns/preflight?async=1&request_id=" + uuid.uuid4().hex
        assigned = {"id": "assigned", "host": "proxy.invalid", "port": 1234,
                    "username": "fixture-user", "password": "fixture-secret"}
        observed = []

        class FakeBridge:
            def __init__(self, proxy):
                self.proxy = proxy
                self.address = "http://127.0.0.1:preflight"

            def close(self):
                pass

        session = Mock(email="fixture@example.invalid")
        def ensure_auth():
            observed.append(registration_proxy.endpoint())
            return {"organizations": [{"resourceKey": server.config.ORG_KEY}]}
        session.ensure_auth.side_effect = ensure_auth
        with patch.object(server, "_resolve_org", self.real_resolve), \
             patch.object(server.Session, "from_email", return_value=session), \
             patch.object(registration_proxy, "assign", return_value=assigned) as assign, \
             patch.object(registration_proxy, "TunnelBridge", FakeBridge), \
             patch.object(server.org_policy, "ensure_membership", return_value=server.config.ORG_KEY), \
             patch.object(server, "_cache_org_key"):
            response = self.finish()

        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(response.json["ok"], response.json)
        # Org validation and catalog loading are separate worker phases, each
        # gets the same account's assigned route. Nested work within a phase
        # reuses that phase's route rather than assigning again.
        self.assertEqual(assign.call_count, 2)
        self.assertEqual([item.args for item in assign.call_args_list], [
            ("fixture@example.invalid", ""), ("fixture@example.invalid", "")])
        self.assertEqual(observed, ["http://127.0.0.1:preflight"])
        self.runner.start.assert_not_called()

    def test_clip_review_uses_catalog_without_reading_sensor_sources(self):
        from moneymin import imu_coverage
        clips = [{"clip_uid": "fixture-clip", "parent_video_uid": "fixture-parent",
                  "window_s": [0, 300], "dur_s": 300, "source": "ego4d"}]
        self.body.update(include_clip_plan=True, content_mode="dataset")
        self.stack.enter_context(patch.object(server.campaign.task_matching, "rule_for", return_value=object()))
        self.stack.enter_context(patch.object(server.campaign, "_compatible_task_clips", return_value=clips))
        self.stack.enter_context(patch.object(imu_coverage, "refine_candidates",
                                             side_effect=AssertionError("preview read real IMU")))
        self.stack.enter_context(patch.object(server.campaign, "_clip_is_cached",
                                             side_effect=AssertionError("preview opened media")))
        response = self.finish()
        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(response.json["ok"], response.json)
        self.assertTrue(response.json["preflight_id"])
        self.assertTrue(response.json["capacity"]["requires_measured_validation"])
        self.assertFalse(response.json["capacity"]["campaign_ready"])
        self.runner.start.assert_not_called()

    def test_invalid_request_id_is_rejected_before_remote_work(self):
        for request_id in ("", "short", "g" * 32, "a" * 33):
            response = self.client.post("/api/campaigns/preflight?async=1&request_id=" + request_id, json=self.body)
            self.assertEqual(response.status_code, 400)
        self.resolve.assert_not_called()

    def test_shutdown_waits_for_background_account_work(self):
        self.client.post(self.url, json=self.body)
        self.assertTrue(self.entered.wait(1))
        self.assertFalse(self.client.post("/api/campaigns/drain").json["ready"])
        self.release.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.client.post("/api/campaigns/drain").json["ready"]:
                self.finished.set()
                break
            time.sleep(.01)
        else:
            self.fail("shutdown did not finish after the verification worker")
        self.assertEqual(self.client.post(self.url, json=self.body).status_code, 409)
        self.runner.start.assert_not_called()
