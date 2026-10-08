"""Campaign Minute calls must stay inside the account's assigned route."""
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from moneymin import campaign, config, registration_proxy
from moneymin.campaign_types import AccountSpec


ACCOUNT = "fixture@example.invalid"
ORG = config.ORG_KEY


class FakeBridge:
    opened = []

    def __init__(self, proxy):
        self.proxy = proxy
        self.address = f"http://127.0.0.1:{len(self.opened) + 1}"
        self.closed = False
        self.opened.append(self)

    def close(self):
        self.closed = True


class CampaignProxyRouteTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.proxy = {"id": "assigned", "host": "proxy.invalid", "port": 1080,
                      "username": "route-user", "password": "route-secret"}
        FakeBridge.opened = []
        self.stack.enter_context(patch.object(registration_proxy, "assign", return_value=self.proxy))
        self.stack.enter_context(patch.object(registration_proxy, "TunnelBridge", FakeBridge))

    def test_task_fetch_authentication_and_catalog_use_assigned_route(self):
        events = []

        class FakeSession:
            _live = False

            def ensure_auth(self, *, org_key):
                self.assert_route("auth")
                events.append("auth")

            def all_tasks(self, org_key):
                self.assert_route("tasks")
                events.append("tasks")
                return [{"id": "future", "name": "Unknown task"}]

            @staticmethod
            def assert_route(phase):
                if registration_proxy.endpoint() is None:
                    raise AssertionError(f"{phase} escaped the account route")

        with patch.object(campaign.Session, "from_email", side_effect=lambda email: (
                FakeSession() if registration_proxy.endpoint() else
                (_ for _ in ()).throw(AssertionError("refresh escaped the account route")))) as load, \
                patch.object(campaign, "_compatible_task_clips", return_value=[]):
            rows = campaign.available_tasks(ACCOUNT, ORG, include_unavailable=True)
        self.assertEqual(events, ["auth", "tasks"])
        self.assertEqual(rows[0]["id"], "future")
        load.assert_called_once_with(ACCOUNT)
        self.assertEqual(registration_proxy.assign.call_args.args, (ACCOUNT, ""))
        self.assertTrue(FakeBridge.opened[-1].closed)
        self.assertIsNone(registration_proxy.endpoint())

    def test_injected_remote_task_rows_do_not_open_a_route(self):
        with patch.object(registration_proxy, "assign") as assign, \
                patch.object(campaign, "_compatible_task_clips", return_value=[]):
            rows = campaign.available_tasks(ACCOUNT, ORG, remote_tasks=[],
                                             include_unavailable=True)
        self.assertEqual(rows, [])
        assign.assert_not_called()

    def test_real_session_cache_with_wrong_owner_fails_before_route_or_upload(self):
        wrong = campaign._SESSION_TYPE({"email": "other@example.invalid",
                                        "idToken": "fixture-token"})
        with patch.object(registration_proxy, "assign") as assign, \
                patch.object(campaign, "upload_session") as send:
            result = campaign.upload_to_account(
                {"duration_ms": 60_000}, AccountSpec(ACCOUNT, ORG),
                "task", 30, True, True, session_cache={ACCOUNT: wrong})
        self.assertFalse(result["ok"])
        self.assertTrue(result["access_error"])
        self.assertIn("identidade", result["error"].casefold())
        assign.assert_not_called()
        send.assert_not_called()

    def test_session_result_refresh_and_get_use_assigned_route(self):
        events = []

        class FakeSession:
            def get(self, path):
                self.assert_route()
                events.append(path)
                return 200, {"taskName": "Task", "files": [], "unprocessedFiles": []}

            @staticmethod
            def assert_route():
                if registration_proxy.endpoint() is None:
                    raise AssertionError("session GET escaped the account route")

        with patch.object(campaign.Session, "from_email", side_effect=lambda email: (
                FakeSession() if registration_proxy.endpoint() else
                (_ for _ in ()).throw(AssertionError("refresh escaped the account route")))) as load:
            result = campaign.session_result(ACCOUNT, ORG, "existing-session")
        self.assertEqual(result["session_id"], "existing-session")
        self.assertEqual(events, [f"/api/v1/organizations/{ORG}/sessions/existing-session"])
        load.assert_called_once_with(ACCOUNT)
        self.assertTrue(FakeBridge.opened[-1].closed)
        self.assertIsNone(registration_proxy.endpoint())

    def test_pending_recovery_uses_route_and_restores_outer_identity(self):
        outer = {"id": "outer", "host": "outer.invalid", "port": 1081,
                 "username": "outer-user", "password": "outer-secret"}
        events = []

        def pump(*args, **kwargs):
            self.assertIsNotNone(registration_proxy.endpoint())
            events.append(kwargs["session_ids"])
            return []

        with registration_proxy.route(outer):
            outer_endpoint = registration_proxy.endpoint()
            with patch.object(campaign, "pump_pending", side_effect=pump):
                self.assertEqual(campaign._pump_account_pending(
                    Mock(), ACCOUNT, ORG, session_ids={"existing-session"}), [])
            self.assertEqual(registration_proxy.endpoint(), outer_endpoint)
        self.assertEqual(events, [{"existing-session"}])
        self.assertIsNone(registration_proxy.endpoint())

    def test_full_upload_auth_recovery_and_minute_lifecycle_share_route(self):
        events = []
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "fixture.mp4"
            video.write_bytes(b"offline fixture")
            profile = SimpleNamespace(device_id="fixture-device", frames_gop=30,
                                      uptime_ns_at=lambda _wall: 123)

            class FakeSession:
                _live = False
                _moneymin_pending_pumped = False
                recording_policy = None

                def ensure_auth(self, *, org_key):
                    self.assert_route("auth")
                    events.append("auth")

                @staticmethod
                def assert_route(phase):
                    if registration_proxy.endpoint() is None:
                        raise AssertionError(f"{phase} escaped the account route")

            def from_email(email):
                self.assertEqual(email, ACCOUNT)
                FakeSession.assert_route("refresh")
                events.append("refresh")
                return FakeSession()

            def pending(*_args, **_kwargs):
                FakeSession.assert_route("recovery")
                events.append("recovery")
                return []

            def lifecycle(_session, _path, _org, **kwargs):
                FakeSession.assert_route("upload/evaluate/finalize")
                events.extend(["upload", "evaluate", "finalize"])
                self.assertTrue(kwargs["evaluate"])
                self.assertTrue(kwargs["finalize"])
                chunk = SimpleNamespace(state="done", upload_id="fixture-upload",
                                        evaluate_result={"checks": []}, error=None)
                return SimpleNamespace(session_id=kwargs["session_id"], finalized=True,
                                       finalize_status=204, chunks=[chunk])

            with ExitStack() as stack:
                stack.enter_context(patch.object(campaign.Session, "from_email", side_effect=from_email))
                stack.enter_context(patch.object(campaign.device_profile, "get_profile", return_value=profile))
                stack.enter_context(patch.object(campaign, "pump_pending", side_effect=pending))
                stack.enter_context(patch.object(campaign, "list_sidecars", return_value=[]))
                stack.enter_context(patch.object(campaign, "_new_identity",
                                                 return_value=("fixture-session", "fixture-session_0", "2026-01-01T00:00:00Z")))
                stack.enter_context(patch.object(campaign, "probe_video",
                                                 return_value={"fps": 30, "duration_ms": 60_000}))
                stack.enter_context(patch.object(campaign, "build_frames_csv_from_video",
                                                 return_value="frames-fixture"))
                stack.enter_context(patch.object(campaign, "_build_sidecar", return_value=b"sidecar"))
                stack.enter_context(patch.object(campaign, "_chunk_plan", return_value=[(0, 60_000)]))
                stack.enter_context(patch.object(campaign.device_profile, "recorded_at_to_wall_ms",
                                                 return_value=1000))
                upload_mock = stack.enter_context(
                    patch.object(campaign, "upload_session", side_effect=lifecycle))
                result = campaign.upload_to_account(
                    {"duration_ms": 60_000, "video_path": str(video),
                     "probe": {"fps": 30, "duration_ms": 60_000},
                     "imu_csv": "sample", "frames_csv": "prepared"},
                    AccountSpec(ACCOUNT, ORG), "task", 30, True, True)
                successful_events = list(events)

                route_calls = 0
                original_route = campaign.account_registration_route

                @contextmanager
                def fail_upload_route_close(email):
                    nonlocal route_calls
                    route_calls += 1
                    with original_route(email):
                        yield
                    if route_calls == 3:
                        raise campaign._AccountRouteError("cleanup")

                with patch.object(campaign, "account_registration_route",
                                  side_effect=fail_upload_route_close):
                    calls_before_close = upload_mock.call_count
                    close_result = campaign.upload_to_account(
                        {"duration_ms": 60_000, "video_path": str(video),
                         "probe": {"fps": 30, "duration_ms": 60_000},
                         "imu_csv": "sample", "frames_csv": "prepared"},
                        AccountSpec(ACCOUNT, ORG), "task", 30, True, True)
                    calls_after_close = upload_mock.call_count

                class CloseFailureBridge(FakeBridge):
                    def close(self):
                        self.closed = True
                        if len(self.opened) == 3:
                            raise OSError("route-secret-must-not-leak")

                FakeBridge.opened = []
                with patch.object(registration_proxy, "TunnelBridge", CloseFailureBridge), \
                        patch.object(campaign, "upload_session",
                                     side_effect=campaign.UploadError(
                                         "Avaliação inconclusiva; recibo preservado.",
                                         phase="evaluation", review_required=True)):
                    review_result = campaign.upload_to_account(
                        {"duration_ms": 60_000, "video_path": str(video),
                         "probe": {"fps": 30, "duration_ms": 60_000},
                         "imu_csv": "sample", "frames_csv": "prepared"},
                        AccountSpec(ACCOUNT, ORG), "task", 30, True, True)

                def partial_lifecycle(_session, _path, _org, **kwargs):
                    return SimpleNamespace(
                        session_id=kwargs["session_id"], finalized=False,
                        finalize_status=503,
                        chunks=[SimpleNamespace(state="done", upload_id="fixture-upload",
                                                evaluate_result={"checks": []}, error=None)])

                route_calls = 0
                with patch.object(campaign, "account_registration_route",
                                  side_effect=fail_upload_route_close), \
                        patch.object(campaign, "upload_session", side_effect=partial_lifecycle):
                    partial_result = campaign.upload_to_account(
                        {"duration_ms": 60_000, "video_path": str(video),
                         "probe": {"fps": 30, "duration_ms": 60_000},
                         "imu_csv": "sample", "frames_csv": "prepared"},
                        AccountSpec(ACCOUNT, ORG), "task", 30, True, True)

        self.assertTrue(result["ok"], result)
        self.assertEqual(successful_events, ["refresh", "auth", "recovery", "upload", "evaluate", "finalize"])
        self.assertTrue(close_result["ok"], close_result)
        self.assertTrue(close_result["finalized"])
        self.assertEqual(close_result["session_id"], "fixture-session")
        self.assertEqual(close_result["uploads"], ["fixture-upload"])
        self.assertIn("não foi possível encerrar", close_result["warning"].casefold())
        self.assertEqual(calls_after_close - calls_before_close, 1)
        self.assertFalse(review_result["ok"])
        self.assertEqual(review_result["session_id"], "fixture-session")
        self.assertEqual(review_result["error"],
                         "Avaliação inconclusiva; recibo preservado.")
        self.assertFalse(review_result["retryable"])
        self.assertNotIn("route-secret-must-not-leak", review_result["error"])
        self.assertFalse(partial_result["ok"])
        self.assertFalse(partial_result["finalized"])
        self.assertEqual(partial_result["session_id"], "fixture-session")
        self.assertIn("não finalizada", partial_result["error"])
        self.assertTrue(all(bridge.closed for bridge in FakeBridge.opened))
        self.assertIsNone(registration_proxy.endpoint())

    def test_restricted_auth_error_survives_route_close_failure(self):
        class CloseFailureBridge(FakeBridge):
            def close(self):
                self.closed = True
                raise OSError("route-secret-must-not-leak")

        original = campaign.AuthError("Conta restrita pelo serviço.", code="restricted")
        with patch.object(registration_proxy, "TunnelBridge", CloseFailureBridge):
            with self.assertRaises(campaign.AuthError) as raised:
                with campaign.account_registration_route(ACCOUNT):
                    raise original
        self.assertIs(raised.exception, original)
        self.assertEqual(raised.exception.account_issue_code, "restricted")
        self.assertIsNone(registration_proxy.endpoint())
        self.assertIsNone(registration_proxy._ACCOUNT_OWNER.get())

    def test_route_setup_failure_is_safe_and_blocks_auth_or_upload(self):
        with patch.object(registration_proxy, "TunnelBridge",
                          side_effect=OSError("route-secret-must-not-leak")), \
                patch.object(campaign.Session, "from_email") as load, \
                patch.object(campaign, "upload_session") as send:
            result = campaign.upload_to_account(
                {"duration_ms": 60_000}, AccountSpec(ACCOUNT, ORG),
                "task", 30, True, True)
        self.assertFalse(result["ok"])
        self.assertTrue(result["access_error"])
        self.assertTrue(result["retryable"], result)
        self.assertNotIn("route-secret-must-not-leak", result["error"])
        self.assertNotIn("session_id", result)
        load.assert_not_called()
        send.assert_not_called()
        self.assertIsNone(registration_proxy.endpoint())


if __name__ == "__main__":
    unittest.main()
