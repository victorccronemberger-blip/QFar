"""Offline contracts for Minute 1.29.0 (Galaxy S22 SM-S901E) alignment.

Drives shipped builders/validators/clients. No network. Fixtures are structural,
not device-acquired sensor captures.
"""
from __future__ import annotations

import io
import json
import unittest
import zipfile
from unittest.mock import patch

from moneymin import config
from moneymin.device_profile import DeviceProfile
from moneymin.minute_api import (
    _tasks_lang_query,
    categories_from_tasks,
    minute_lang_code,
)
from moneymin.sidecar import build_metadata_json, build_sidecar_zip
from moneymin.upload import complete_upload
from moneymin.validate import summarize, validate_sidecar_zip


class Minute129DefaultsTests(unittest.TestCase):
    def test_config_identity_defaults_are_1_29_s22(self):
        self.assertEqual(config.APP_VERSION, "1.29.0")
        self.assertEqual(config.ANDROID_VERSION_CODE, "1004038")
        self.assertEqual(config.NATIVE_DEVICE_MODEL, "SM-S901E")
        self.assertEqual(config.NATIVE_SIDECAR_MODEL, "SM-S901E")

    def test_device_profile_dataclass_defaults_are_s22(self):
        profile = DeviceProfile(email="fixture@example.com", device_id="dev-fixture")
        self.assertEqual(profile.device_model, "SM-S901E")
        self.assertEqual(profile.sidecar_model, "SM-S901E")
        self.assertEqual(profile.sidecar_platform_meta(), {"os": "android", "version": 34})
        self.assertEqual(profile.upload_platform_meta(), {"os": "android"})
        self.assertEqual(profile.upload_device_meta(), {"model": "SM-S901E"})


class Minute129LangAndCategoriesTests(unittest.TestCase):
    def test_lang_code_maps_accept_language_to_app_i18n(self):
        with patch.object(config, "ACCEPT_LANGUAGE", "pt-BR,pt;q=0.9"):
            self.assertEqual(minute_lang_code(), "pt")
            self.assertEqual(_tasks_lang_query(), "?langCode=pt")
        with patch.object(config, "ACCEPT_LANGUAGE", "en-US,en;q=0.9"):
            self.assertEqual(minute_lang_code(), "")
            self.assertEqual(_tasks_lang_query(), "")
        with patch.object(config, "ACCEPT_LANGUAGE", "es-ES,es;q=0.9"):
            self.assertEqual(minute_lang_code(), "es")
            self.assertEqual(_tasks_lang_query(), "?langCode=es")

    def test_categories_from_tasks_aggregates_embedded_slugs(self):
        tasks = [
            {"id": "t1", "name": "A", "categories": [
                {"slug": "cooking", "label": "Cooking"},
                {"slug": "kitchen", "label": "Kitchen"},
            ]},
            {"id": "t2", "name": "B", "categories": [
                {"slug": "cooking", "label": "Cooking"},
            ]},
        ]
        cats = categories_from_tasks(tasks)
        slugs = sorted(c["slug"] for c in cats)
        self.assertEqual(slugs, ["cooking", "kitchen"])
        cooking = next(c for c in cats if c["slug"] == "cooking")
        self.assertEqual(cooking["label"], "Cooking")
        self.assertEqual(cooking["name"], "Cooking")


class Minute129SidecarBuilderTests(unittest.TestCase):
    def test_bare_builder_defaults_match_s22_anchor_without_device_meta(self):
        """No-override path: model comes from NATIVE_SIDECAR_MODEL, not calib JSON."""
        meta = build_metadata_json(
            session_id="bare-session",
            chunk_index=0,
            duration_ms=1000,
            recorded_at="2026-10-03T00:00:00.000Z",
            log_id="bare-session_0",
        )
        self.assertEqual(meta["device"]["model"], "SM-S901E")
        self.assertEqual(meta["device"]["model"], config.NATIVE_SIDECAR_MODEL)
        self.assertEqual(meta["platform"]["os"], "android")
        self.assertNotIn("type", meta["platform"])
        self.assertEqual(meta["appVersion"], "1.29.0")
        self.assertTrue(
            str(meta["cameras"][0]["name"]).startswith("camera_logical_"),
        )
        # Intrinsics pulled from SM-S901E calib entry (fx 1465 @ 4032 → scaled).
        self.assertAlmostEqual(meta["cameras"][0]["intrinsics"]["fx"],
                               1465.0 * (1440 / 4032), places=3)

        zip_bytes = build_sidecar_zip(
            session_id="bare-session",
            chunk_index=0,
            duration_ms=1000,
            recorded_at="2026-10-03T00:00:00.000Z",
            log_id="bare-session_0",
        )
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            zip_meta = json.loads(archive.read("bare-session_0.metadata.json"))
        self.assertEqual(zip_meta["device"]["model"], "SM-S901E")
        self.assertEqual(zip_meta["platform"]["os"], "android")
        # Create-upload short meta must agree with sidecar model when no profile.
        create_model = DeviceProfile(
            email="bare@example.com", device_id="bare-dev",
        ).upload_device_meta()["model"]
        self.assertEqual(create_model, zip_meta["device"]["model"])

        result = validate_sidecar_zip(
            zip_bytes, log_id="bare-session_0", duration_ms=1000)
        self.assertEqual(summarize(result)["counts"].get("fail", 0), 0)

    def test_builder_emits_1_29_zip_members_csv_and_platform_os(self):
        zip_bytes = build_sidecar_zip(
            session_id="native-session",
            chunk_index=0,
            duration_ms=1000,
            recorded_at="2026-10-03T00:00:00.000Z",
            device_meta={"model": "SM-S901E", "systemName": "Android",
                         "systemVersion": "14"},
            platform_meta={"os": "android", "version": 34},
            log_id="native-session_0",
        )
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            names = set(archive.namelist())
            self.assertEqual(names, {
                "native-session_0.metadata.json",
                "native-session_0.imu.csv",
                "native-session_0.frames.csv",
            })
            metadata = json.loads(archive.read("native-session_0.metadata.json"))
            imu = archive.read("native-session_0.imu.csv").decode("utf-8")
            frames = archive.read("native-session_0.frames.csv").decode("utf-8")
        self.assertEqual(metadata["appVersion"], "1.29.0")
        self.assertEqual(metadata["platform"], {"os": "android", "version": 34})
        self.assertNotIn("type", metadata["platform"])
        self.assertEqual(metadata["timebase"]["clockDomain"],
                         "android_elapsedRealtimeNanos")
        self.assertEqual(metadata["imuDiagnostics"]["strategy"], "gyro_anchored_v1")
        self.assertNotIn("clockOffsetNs", metadata["imuDiagnostics"])
        self.assertEqual(imu.splitlines()[0], "t,ax,ay,az,wx,wy,wz")
        self.assertEqual(frames.splitlines()[0], "i,ptsNs,dtNs,tNs,key")
        result = validate_sidecar_zip(
            zip_bytes, log_id="native-session_0", duration_ms=1000)
        summary = summarize(result)
        self.assertEqual(summary["counts"].get("fail", 0), 0, summary)

    def test_validator_fails_when_required_member_missing(self):
        meta = build_metadata_json(
            session_id="native-session",
            chunk_index=0,
            duration_ms=1000,
            recorded_at="2026-10-03T00:00:00.000Z",
            log_id="native-session_0",
            platform_meta={"os": "android", "version": 34},
            device_meta={"model": "SM-S901E", "systemName": "Android",
                         "systemVersion": "14"},
        )
        broken = io.BytesIO()
        with zipfile.ZipFile(broken, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("native-session_0.metadata.json", json.dumps(meta))
            archive.writestr("native-session_0.imu.csv",
                             "t,ax,ay,az,wx,wy,wz\n0,0,0,9.81,0,0,0\n")
            # frames.csv omitted on purpose
        checks = {c.name: c for c in validate_sidecar_zip(
            broken.getvalue(), log_id="native-session_0", duration_ms=1000)}
        self.assertEqual(checks["zip"].status, "fail")
        self.assertIn("frames.csv", checks["zip"].detail)

    def test_validator_fails_clockOffsetNs_in_sidecar_imu_diagnostics(self):
        zip_bytes = build_sidecar_zip(
            session_id="native-session",
            chunk_index=0,
            duration_ms=1000,
            recorded_at="2026-10-03T00:00:00.000Z",
            log_id="native-session_0",
            platform_meta={"os": "android", "version": 34},
            device_meta={"model": "SM-S901E", "systemName": "Android",
                         "systemVersion": "14"},
        )
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            meta = json.loads(archive.read("native-session_0.metadata.json"))
            imu = archive.read("native-session_0.imu.csv")
            frames = archive.read("native-session_0.frames.csv")
        meta["imuDiagnostics"]["clockOffsetNs"] = "123"
        poisoned = io.BytesIO()
        with zipfile.ZipFile(poisoned, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("native-session_0.imu.csv", imu)
            archive.writestr("native-session_0.frames.csv", frames)
            archive.writestr("native-session_0.metadata.json", json.dumps(meta))
        checks = {c.name: c for c in validate_sidecar_zip(
            poisoned.getvalue(), log_id="native-session_0", duration_ms=1000)}
        self.assertEqual(checks["imuDiagnostics.no_clockOffsetNs"].status, "fail")

    def test_legacy_platform_type_warns_but_os_passes(self):
        zip_bytes = build_sidecar_zip(
            session_id="native-session",
            chunk_index=0,
            duration_ms=1000,
            recorded_at="2026-10-03T00:00:00.000Z",
            log_id="native-session_0",
            platform_meta={"type": "android", "version": 34},
            device_meta={"model": "SM-S901E", "systemName": "Android",
                         "systemVersion": "14"},
        )
        checks = {c.name: c for c in validate_sidecar_zip(
            zip_bytes, log_id="native-session_0", duration_ms=1000)}
        # platform check name may vary; find the platform.* check
        platform_checks = [c for n, c in checks.items() if "platform" in n]
        self.assertTrue(platform_checks)
        self.assertTrue(any(c.status == "warn" for c in platform_checks))


class Minute129UploadCompleteTests(unittest.TestCase):
    def test_complete_upload_body_includes_1_29_flags(self):
        captured: dict = {}

        class FakeSession:
            def request_detailed(self, method, path, body=None, **_kwargs):
                captured["method"] = method
                captured["path"] = path
                captured["body"] = body
                return 200, "{}", {}, None

            # Some call paths use request(); support both.
            def request(self, method, path, body=None, **kwargs):
                return self.request_detailed(method, path, body, **kwargs)

        # Prefer the helper used by complete_upload internals
        from moneymin import upload as upload_mod

        def fake_session_request(session, method, path, body=None, **_kwargs):
            captured["method"] = method
            captured["path"] = path
            captured["body"] = body
            return 204, "", {}

        with patch.object(upload_mod, "_session_request", fake_session_request):
            complete_upload(
                FakeSession(),
                "upload-id",
                1234,
                suppress_per_chunk_catbear=True,
                session_complete=True,
                network_type="wifi",
            )
        self.assertEqual(captured["method"], "PATCH")
        self.assertEqual(captured["path"], "/api/v1/uploads/upload-id/complete")
        self.assertEqual(captured["body"]["size_bytes"], 1234)
        self.assertIs(captured["body"]["suppress_per_chunk_catbear"], True)
        self.assertIs(captured["body"]["session_complete"], True)
        self.assertEqual(captured["body"]["network_type"], "wifi")


if __name__ == "__main__":
    unittest.main()
