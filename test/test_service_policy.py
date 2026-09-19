import json
import unittest
from datetime import datetime, timedelta, timezone
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from moneymin import config, minute_api, vpn
from moneymin.minute_api import AuthError, Session
from moneymin.service_policy import RecordingPolicy
from moneymin.web.account_issues import account_issue


POLICY = {"configVersion": 1, "minDurationMs": 1000,
          "maxDurationMs": 10000, "backlogCapMs": 60000}


class RecordingPolicyTests(unittest.TestCase):
    def test_valid_policy_is_immutable_and_does_not_alias_payload(self):
        payload = dict(POLICY)
        policy = RecordingPolicy.parse(payload)
        payload["maxDurationMs"] = 5
        self.assertEqual(policy.max_duration_ms, 10000)
        snapshot = policy.limits()
        snapshot["max_duration_ms"] = 3
        self.assertEqual(policy.max_duration_ms, 10000)

    def test_malformed_contracts_are_rejected_without_coercion(self):
        for payload in ([], {}, {**POLICY, "minDurationMs": True},
                        {**POLICY, "maxDurationMs": "10000"},
                        {**POLICY, "minDurationMs": 20000},
                        {**POLICY, "backlogCapMs": 0},
                        {**POLICY, "configVersion": 0}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                RecordingPolicy.parse(payload)

    def test_backlog_and_future_recordings_are_rejected(self):
        policy = RecordingPolicy.parse(POLICY)
        now = datetime(2026, 9, 18, tzinfo=timezone.utc)
        policy.validate_recording_time((now - timedelta(seconds=3)).isoformat(), 2000, now.timestamp())
        for value in (None, "invalid", "2026-09-18T00:00:00",
                      (now - timedelta(seconds=61)).isoformat(), now.isoformat()):
            with self.subTest(value=value), self.assertRaises(ValueError):
                policy.validate_recording_time(value, 2000, now.timestamp())


class SessionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(vpn, "ENFORCE", False))
        self.stack.enter_context(patch.object(config, "REQUIRE_CURL", False))
        self.clock = self.stack.enter_context(patch.object(minute_api.time, "monotonic", return_value=100.0))
        self.stack.enter_context(patch.object(minute_api.device_profile, "get_profile",
                                            return_value=SimpleNamespace(device_model="fixture")))
        self.session = Session({"idToken": "fixture", "email": "test@example.invalid"})
        self.fetch = self.stack.enter_context(patch.object(self.session, "fetch_recording_config",
                                                         return_value=(200, json.dumps(POLICY))))
        self.camera_patch = patch.object(self.session, "camera_model_allowed", return_value=True)
        self.camera = self.stack.enter_context(self.camera_patch)
        self.stack.enter_context(patch.object(self.session, "version_gate", return_value=None))
        self.state = self.stack.enter_context(patch.object(self.session, "org_state", return_value={
            "blocked": False, "userState": "active", "cameraSources": ["native"]}))
        self.geo = self.stack.enter_context(patch.object(self.session, "recording_geo", return_value={
            "recordingAuthorization": "APPROVED", "canUpload": True, "blockedReason": None}))
        self.opened = self.stack.enter_context(patch.object(self.session, "app_opened"))

    def check(self, body=None):
        self.session._check_write_policy("POST", "/api/v1/uploads?org_key=fixture",
                                         body or {"duration_ms": 2000, "meta": {"source": "native"},
                                                  "recorded_at": (datetime.now(timezone.utc) - timedelta(seconds=3)).isoformat()})

    def test_success_is_cached_and_no_analytics_is_published(self):
        self.session.warmup()
        self.session.warmup()
        self.fetch.assert_called_once()
        self.opened.assert_not_called()

    def test_failure_is_retried_after_backoff(self):
        self.fetch.side_effect = [(503, "unavailable"), (200, json.dumps(POLICY))]
        self.session.warmup()
        self.assertIn("recording_config", self.session.initialization_errors)
        with self.assertRaises(AuthError):
            self.check()
        self.assertEqual(self.fetch.call_count, 1)
        self.clock.return_value = 106.0
        self.check()
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual(self.session.initialization_errors, {})

    def test_expired_policy_cannot_authorize_after_refresh_failure(self):
        self.check()
        self.clock.return_value = 161.0
        self.fetch.return_value = (200, "[]")
        with self.assertRaises(AuthError) as caught:
            self.check()
        self.assertEqual(caught.exception.account_issue_code, "service")

    def test_remote_limits_are_isolated_even_for_same_email(self):
        original = config.recording_limits()
        self.session.warmup()
        other = Session(dict(self.session.data))
        with patch.object(other, "fetch_recording_config", return_value=(200, json.dumps({**POLICY, "maxDurationMs": 3000}))), \
             patch.object(other, "camera_model_allowed", return_value=True):
            other.warmup()
        self.assertEqual(self.session.recording_policy.max_duration_ms, 10000)
        self.assertEqual(other.recording_policy.max_duration_ms, 3000)
        self.assertEqual(config.recording_limits(), original)

    def test_identity_change_invalidates_previous_policy(self):
        self.session.warmup()
        self.session.with_email("other@example.invalid")
        self.assertIsNone(self.session.recording_policy)
        self.assertIsNone(self.session._recording_checked_at)
        self.assertIsNone(self.session.device_camera_allowed)

    def test_policy_issue_is_not_a_ban_or_password_failure(self):
        issue = account_issue("fixture@example.invalid", AuthError("private diagnostic", code="policy"))
        self.assertEqual(issue["code"], "policy")
        self.assertFalse(issue["restriction_confirmed"])
        self.assertFalse(issue["retryable"])

    def test_http_error_cannot_supply_an_authorizing_policy(self):
        camera = {"policyVersion": 1, "iosAllowModels": [], "iosDeniedModels": [],
                  "androidAllowModels": ["fixture"], "androidAllowModelPatterns": [],
                  "normalization": {
                      "trimWhitespace": True, "lowercase": False, "collapseInternalWhitespace": True}}
        with patch.object(self.session, "get", return_value=(503, json.dumps(camera))):
            self.assertEqual(self.session.camera_policy(), {})
        with patch.object(self.session, "get", return_value=(500, '{"userState":"active"}')):
            self.assertEqual(self.session.quality_state("fixture"), {})

    def test_invalid_camera_policy_is_unknown_not_permission(self):
        with patch.object(self.session, "get", return_value=(200, '{"policyVersion":true}')):
            self.assertEqual(self.session.camera_policy(), {})
        incomplete = {"policyVersion": 1, "androidAllowModels": ["x"],
                      "androidAllowModelPatterns": [], "normalization": {
                          "trimWhitespace": True, "lowercase": True,
                          "collapseInternalWhitespace": True}}
        with patch.object(self.session, "get", return_value=(200, json.dumps(incomplete))):
            self.assertEqual(self.session.camera_policy(), {})

    def test_camera_decision_matches_hermes_normalize_and_patterns(self):
        session = Session({"idToken": "fixture", "email": "camera@example.invalid"})
        policy = {
            "policyVersion": 1,
            "iosAllowModels": [],
            "iosDeniedModels": ["iPhone"],
            "androidAllowModels": ["sm-s918b"],
            "androidAllowModelPatterns": [r"pixel\s*8"],
            "normalization": {
                "trimWhitespace": False, "lowercase": False,
                "collapseInternalWhitespace": False,
            },
        }
        with patch.object(session, "get", return_value=(200, json.dumps(policy))):
            # Flags remotas False não desligam a normalização do APK.
            self.assertIs(session.camera_model_allowed("  SM-S918B  "), True)
            self.assertIs(session.camera_model_allowed("Pixel   8 Pro"), True)
            self.assertIs(session.camera_model_allowed("SM-G991B"), False)
            # Deny iOS não afeta decisão Android.
            self.assertIs(session.camera_model_allowed("iPhone"), False)

    def test_error_response_cannot_masquerade_as_a_profile(self):
        with patch.object(self.session, "request", return_value=(503, '{"organizations":[]}')):
            self.assertEqual(self.session.me(), {})

    def test_denied_or_unknown_camera_never_authorizes(self):
        for allowed, code in ((False, "policy"), (None, "service")):
            with self.subTest(allowed=allowed):
                self.session._recording_checked_at = None
                self.session._recording_retry_at = 0
                self.camera.return_value = allowed
                with self.assertRaises(AuthError) as caught:
                    self.check()
                self.assertEqual(caught.exception.account_issue_code, code)

    def test_bad_duration_blocked(self):
        for duration in (True, "2000", 999, 10001):
            with self.subTest(duration=duration), self.assertRaises(AuthError):
                self.check({"duration_ms": duration, "meta": {"source": "native"}})

    def test_org_restrictions_and_sources_are_enforced(self):
        for state in ({"blocked": True, "userState": "on_hold", "cameraSources": ["native"]},
                      {"blocked": False, "userState": "unknown", "cameraSources": ["native"]},
                      {"blocked": False, "userState": "active", "cameraSources": []},
                      {"blocked": False, "userState": "active", "cameraSources": ["external"]}):
            with self.subTest(state=state):
                self.state.return_value = state
                with self.assertRaises(AuthError):
                    self.check()

    def test_vpn_rechecked_after_initialization_for_both_transports(self):
        self.session.warmup()
        with patch.object(vpn, "ENFORCE", True), patch.object(vpn, "vpn_active", return_value=True), \
             patch.object(minute_api, "_request") as plain, patch.object(minute_api, "_request_detailed") as detailed:
            for request in (self.session.request, self.session.request_detailed):
                with self.assertRaises(AuthError):
                    request("GET", "/api/v1/users/me")
            plain.assert_not_called()
            detailed.assert_not_called()

    def test_production_defaults_enforce_vpn_and_curl(self):
        with patch.dict("os.environ", {"MINUTE_VPN_ENFORCE": "", "MINUTE_REQUIRE_CURL": ""}, clear=False):
            self.assertTrue(vpn._env_enabled("MINUTE_VPN_ENFORCE", True))
            self.assertTrue(config._env_enabled("MINUTE_REQUIRE_CURL", True))
        with patch.dict("os.environ", {"MINUTE_VPN_ENFORCE": "0", "MINUTE_REQUIRE_CURL": "off"}):
            self.assertFalse(vpn._env_enabled("MINUTE_VPN_ENFORCE", True))
            self.assertFalse(config._env_enabled("MINUTE_REQUIRE_CURL", True))

    def test_blocked_upload_never_reaches_either_transport(self):
        self.camera.return_value = False
        with patch.object(minute_api, "_request") as plain, patch.object(minute_api, "_request_detailed") as detailed:
            for request in (self.session.request, self.session.request_detailed):
                with self.assertRaises(AuthError):
                    request("POST", "/api/v1/uploads?org_key=fixture", {"duration_ms": 2000})
            plain.assert_not_called()
            detailed.assert_not_called()

    def test_version_latch_requires_apk_detail_shape(self):
        try:
            minute_api._maybe_latch_version_gate(
                '{"detail":{"message":"please update the app","min_version":"9.9.9"}}')
            self.assertFalse(minute_api._version_gate_file().exists())
            minute_api._maybe_latch_version_gate(
                '{"detail":{"error":"app_version_too_old","minVersion":"9.9.9"}}')
            self.assertFalse(minute_api._version_gate_file().exists())
            minute_api._maybe_latch_version_gate(
                '{"detail":{"error":"app_version_too_old","min_version":"not-a-version"}}')
            self.assertFalse(minute_api._version_gate_file().exists())
            minute_api._maybe_latch_version_gate(
                '{"detail":{"error":"app_version_too_old","min_version":"9.9.9"}}')
            data = json.loads(minute_api._version_gate_file().read_text(encoding="utf-8"))
            self.assertEqual(data["minVersion"], "9.9.9")
            self.assertTrue(minute_api._version_gate_blocks())
        finally:
            minute_api._maybe_latch_version_gate("", clear=True)

    def test_latched_version_blocks_all_mutations_not_reads(self):
        try:
            minute_api._maybe_latch_version_gate(
                '{"detail":{"error":"app_version_too_old","min_version":"9.9.9"}}')
            with self.assertRaises(AuthError) as caught:
                self.session._check_write_policy(
                    "POST", "/api/v1/organizations/join", {"code": "X"})
            self.assertEqual(caught.exception.account_issue_code, "version")
            # Leituras seguem liberadas para diagnóstico.
            self.session._check_write_policy("GET", "/api/v1/users/me", None)
        finally:
            minute_api._maybe_latch_version_gate("", clear=True)

    def test_minute_403_errors_are_typed(self):
        self.assertEqual(
            minute_api._classify_minute_403(
                '{"detail":{"error":"app_version_too_old","min_version":"2.0.0"}}'),
            "version")
        self.assertEqual(
            minute_api._classify_minute_403('{"detail":{"error":"device"}}'), "device")
        self.assertEqual(
            minute_api._classify_minute_403('{"detail":{"error":"uber-device"}}'),
            "uber_device")
        self.assertIsNone(minute_api._classify_minute_403('{"detail":{"error":"other"}}'))
        err = minute_api._auth_failure(
            403, '{"detail":{"error":"uber-device"}}', "Consulta")
        self.assertEqual(err.account_issue_code, "uber_device")
        issue = account_issue("fixture@example.invalid", err)
        self.assertEqual(issue["code"], "uber_device")
        self.assertFalse(issue["restriction_confirmed"])

    def test_recording_geo_blocks_upload_before_transport(self):
        cases = (
            ({}, "service"),
            ({"recordingAuthorization": "APPROVED", "canUpload": False,
              "blockedReason": "quota_exceeded"}, "policy"),
            ({"recordingAuthorization": "REQUIRES_LOCATION", "canUpload": True,
              "blockedReason": None}, "policy"),
            ({"recordingAuthorization": "BLOCKED", "canUpload": True,
              "blockedReason": None}, "policy"),
            ({"canUpload": True}, "service"),
            ({"recordingAuthorization": "REQUIRES_LOCATION", "canUpload": False,
              "blockedReason": None}, "policy"),
            ({"recordingAuthorization": "BLOCKED", "canUpload": False,
              "blockedReason": "geo_restricted"}, "policy"),
            ({"recordingAuthorization": "BLOCKED", "canUpload": False,
              "blockedReason": "quota_exceeded"}, "policy"),
        )
        for quota, code in cases:
            with self.subTest(quota=quota):
                self.geo.return_value = quota
                with patch.object(minute_api, "_request") as plain:
                    with self.assertRaises(AuthError) as caught:
                        self.check()
                    self.assertEqual(caught.exception.account_issue_code, code)
                    plain.assert_not_called()

    def test_semver_rejects_malformed_values(self):
        for value in ("1.23", "garbage 99", "1.2.3-beta4", "1..2", "1.2.3\n", None):
            with self.subTest(value=value):
                self.assertIsNone(minute_api._parse_semver_or_none(value))
        self.assertEqual(minute_api._parse_semver_or_none("1.23.4"), (1, 23, 4))

    def test_quota_missing_required_authorization_is_not_cached(self):
        for payload in ({"canUpload": True}, {"recordingAuthorization": "APPROVED"},
                        {"recordingAuthorization": "APPROVED", "canUpload": True}):
            with self.subTest(payload=payload), patch.object(self.session, "get", return_value=(200, json.dumps(payload))):
                self.assertEqual(Session.recording_geo(self.session, "fixture"), {})
                self.assertNotIn("fixture", self.session._quota_cache)

    def test_diagnostic_auth_is_available_with_latched_version(self):
        self.session._live = True
        with patch.object(minute_api, "_version_gate_blocks", return_value=True), \
             patch.object(self.session, "request", return_value=(200, '{"organizations":[]}')):
            self.assertEqual(self.session.ensure_auth(), {"organizations": []})
            with self.assertRaises(AuthError):
                self.session._check_write_policy("POST", "/api/v1/organizations/join", {})

    def test_app_opened_confirms_success_and_resets_on_account_change(self):
        with patch.object(config, "PUBLISH_APP_OPENED", True):
            self.opened.return_value = (503, "unavailable")
            self.session.warmup()
            self.assertFalse(self.session._app_opened_published)
            self.clock.return_value = 161.0
            self.opened.return_value = (204, "")
            self.session.warmup()
            self.assertTrue(self.session._app_opened_published)
            self.session.with_email("other@example.invalid")
            self.assertFalse(self.session._app_opened_published)

    def test_unknown_vpn_state_blocks_before_transport(self):
        with patch.object(vpn, "ENFORCE", True), patch.object(vpn, "vpn_active", return_value=None), \
             patch.object(minute_api, "_request") as transport:
            with self.assertRaises(AuthError) as caught:
                self.session.request("GET", "/api/v1/users/me")
            self.assertEqual(caught.exception.account_issue_code, "service")
            transport.assert_not_called()


class VpnDetectionTests(unittest.TestCase):
    def test_detection_distinguishes_failure_from_no_active_adapter(self):
        for code, output, expected in ((0, "0", False), (0, "1", True),
                                      (1, "0", None), (0, "", None),
                                      (0, "invalid", None), (0, "-1", None)):
            with self.subTest(code=code, output=output), patch.object(
                vpn.subprocess, "run", return_value=SimpleNamespace(returncode=code, stdout=output)
            ) as run:
                self.assertIs(vpn._detect(), expected)
                self.assertIn("$_.Status -eq 'Up'", run.call_args.args[0][-1])
        with patch.object(vpn.subprocess, "run", side_effect=OSError):
            self.assertIsNone(vpn._detect())
