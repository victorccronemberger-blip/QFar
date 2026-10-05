"""Reject-only metadata checks using declared APK-format fixtures.

These are local structural fixtures, not captured Android media or sensor data.
Ll2/o0.c and its MetadataInput constructor establish the nested field types.
No generator, device profile, media executable or remote API is exercised here.
"""
from __future__ import annotations

import copy
import io
import json
import unittest
import zipfile

from moneymin.validate import validate_sidecar_zip, validate_upload_meta


LOG_ID = "fixture-session_0"
FRAMES = "i,ptsNs,dtNs,tNs,key\n0,5000000,0,1000000000,1\n1,1005000000,1000000000,2000000000,0\n"
IMU = "t,ax,ay,az,wx,wy,wz\n1000000000,0,0,9.81,0,0,0\n2000000000,0,0,9.81,0,0,0\n"


def native_fixture():
    return {
        "id": LOG_ID, "logId": LOG_ID, "createdAt": "2026-10-03T00:00:00.000Z",
        "durationMs": 1000, "appVersion": "1.29.0",
        "platform": {"os": "android", "version": 34},
        "device": {"model": "SM-S901E", "systemName": "Android", "systemVersion": "14"},
        "video": {"path": "/fixture/recording.mp4", "width": 1280, "height": 720, "rotationDeg": 90},
        "session": {"id": "fixture-session"},
        "chunk": {"index": 0, "startTimeMs": 1790985600000, "endTimeMs": 1790985601000},
        "source": "ego",
        "timebase": {"clockDomain": "android_elapsedRealtimeNanos",
                     "startNs": "900000000", "endNs": "2100000000",
                     "startSensorTimestampNs": "1000000000", "endSensorTimestampNs": "2000000000",
                     "firstFrameSensorTimestampNs": "1000000000"},
        "imuDiagnostics": {"sampleCount": 2},
        "artifacts": [{"name": name, "remoteFilename": f"{LOG_ID}.{name}.csv", "contentType": "text/csv"}
                      for name in ("imu", "frames")],
        "codecActuals": {},
    }


def sidecar_checks(metadata):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{LOG_ID}.imu.csv", IMU)
        archive.writestr(f"{LOG_ID}.frames.csv", FRAMES)
        archive.writestr(f"{LOG_ID}.metadata.json", json.dumps(metadata))
    return {check.name: check for check in validate_sidecar_zip(
        output.getvalue(), log_id=LOG_ID, duration_ms=1000)}


class MetadataContractTests(unittest.TestCase):
    def assert_field_fails(self, metadata, field):
        checks = sidecar_checks(metadata)
        self.assertEqual(checks[f"metadata_json.{field}"].status, "fail")
        return checks

    def test_native_fixture_is_structurally_valid_without_claiming_capture(self):
        checks = sidecar_checks(native_fixture())
        for field in ("video", "session", "chunk", "source", "appVersion"):
            self.assertEqual(checks[f"metadata_json.{field}"].status, "pass")
        self.assertFalse(any(check.status == "fail" for check in checks.values()))
        self.assertEqual(checks["artifact.cameras_schema"].status, "warn")
        self.assertEqual(checks["codecActuals.schema"].status, "warn")

    def test_nested_objects_must_be_objects_with_required_fields(self):
        for field in ("video", "session", "chunk"):
            for value in (None, [], True, 8, "fixture", {}):
                with self.subTest(field=field, value=value):
                    metadata = native_fixture()
                    metadata[field] = value
                    self.assert_field_fails(metadata, field)

    def test_nested_missing_fields_are_not_silently_defaulted(self):
        required = {"video": ("path", "width", "height", "rotationDeg"),
                    "session": ("id",), "chunk": ("index", "startTimeMs", "endTimeMs")}
        for field, keys in required.items():
            for key in keys:
                with self.subTest(field=field, key=key):
                    metadata = native_fixture()
                    metadata[field].pop(key)
                    before = copy.deepcopy(metadata)
                    self.assert_field_fails(metadata, field)
                    self.assertEqual(metadata, before)

    def test_video_path_and_session_id_are_nonblank_strings(self):
        for field, key in (("video", "path"), ("session", "id")):
            for value in (None, False, 15, [], {}, "", " \t\n"):
                with self.subTest(field=field, value=value):
                    metadata = native_fixture()
                    metadata[field][key] = value
                    self.assert_field_fails(metadata, field)

    def test_video_dimensions_are_positive_jvm_ints(self):
        for key in ("width", "height"):
            for value in (None, True, 720.0, "720", 0, -1, 2**31, [], {}):
                with self.subTest(key=key, value=value):
                    metadata = native_fixture()
                    metadata["video"][key] = value
                    self.assert_field_fails(metadata, "video")

    def test_rotation_is_jvm_int_without_inventing_a_closed_enum(self):
        for value in (None, False, 90.0, "90", -(2**31) - 1, 2**31, [], {}):
            with self.subTest(value=value):
                metadata = native_fixture()
                metadata["video"]["rotationDeg"] = value
                self.assert_field_fails(metadata, "video")
        for value in (-90, 0, 90, 180, 270):
            with self.subTest(valid_rotation=value):
                metadata = native_fixture()
                metadata["video"]["rotationDeg"] = value
                self.assertEqual(sidecar_checks(metadata)["metadata_json.video"].status, "pass")

    def test_chunk_index_is_nonnegative_jvm_int(self):
        for value in (None, True, 0.0, "0", -1, 2**31, [], {}):
            with self.subTest(value=value):
                metadata = native_fixture()
                metadata["chunk"]["index"] = value
                self.assert_field_fails(metadata, "chunk")

    def test_chunk_wall_times_are_jvm_longs_without_float_coercion(self):
        for key in ("startTimeMs", "endTimeMs"):
            for value in (None, True, 1000.0, "1000", -(2**63) - 1, 2**63, [], {}):
                with self.subTest(key=key, value=value):
                    metadata = native_fixture()
                    metadata["chunk"][key] = value
                    self.assert_field_fails(metadata, "chunk")

    def test_wall_time_shape_does_not_infer_a_sensor_or_duration_relationship(self):
        metadata = native_fixture()
        metadata["chunk"].update(startTimeMs=2**53 + 3, endTimeMs=2**53 + 2)
        checks = sidecar_checks(metadata)
        self.assertEqual(checks["metadata_json.chunk"].status, "pass")
        self.assertEqual(checks["frames.csv"].status, "pass")
        self.assertEqual(checks["xcheck.frames_timebase"].status, "pass")

    def test_source_matches_the_ego_writer_and_is_not_camera_source(self):
        for value in (None, False, 1, [], {}, "", " ", "builtin", "external", "trinet"):
            with self.subTest(value=value):
                metadata = native_fixture()
                metadata["source"] = value
                self.assert_field_fails(metadata, "source")
        metadata = native_fixture()
        metadata["cameras"] = [{"name": "fixture-external", "source": "external"}]
        metadata["timebase"].update(clockDomain="trinet_camera_monotonic",
                                    startNs="1000000000", endNs="2000000000")
        checks = sidecar_checks(metadata)
        self.assertEqual(checks["metadata_json.source"].status, "pass")
        self.assertFalse(any(check.status == "fail" for check in checks.values()))

    def test_app_version_is_nonblank_text_without_fabricating_version(self):
        for value in (None, False, 1.28, [], {}, "", " \t\n"):
            with self.subTest(value=value):
                metadata = native_fixture()
                metadata["appVersion"] = value
                self.assert_field_fails(metadata, "appVersion")
        for value in ("1.28.0", "1.28.0-dev", "1.22.0"):
            with self.subTest(valid_version=value):
                metadata = native_fixture()
                metadata["appVersion"] = value
                self.assertEqual(sidecar_checks(metadata)["metadata_json.appVersion"].status, "pass")

    def test_native_relative_and_absolute_paths_and_extra_fields_remain_valid(self):
        for path in (f"{LOG_ID}.mp4", "/fixture/path with spaces/recording.mp4"):
            with self.subTest(path=path):
                metadata = native_fixture()
                metadata["video"].update(path=path, futureField={"fixture": True})
                metadata["session"]["futureField"] = []
                metadata["chunk"]["futureField"] = "fixture"
                checks = sidecar_checks(metadata)
                self.assertFalse(any(check.status == "fail" for check in checks.values()))

    def test_legacy_platform_type_stays_warning_with_valid_nested_metadata(self):
        metadata = native_fixture()
        metadata["platform"] = {"type": "android", "version": 34}
        metadata["appVersion"] = "1.22.0"
        checks = sidecar_checks(metadata)
        self.assertEqual(checks["platform.android"].status, "warn")
        self.assertFalse(any(check.status == "fail" for check in checks.values()))

    def test_upload_short_device_model_must_be_nonblank_string(self):
        for value in (None, False, True, 18, [], {}, "", " \t\n"):
            with self.subTest(value=value):
                meta = {"logId": LOG_ID, "durationMs": 1000,
                        "platform": {"os": "android"}, "device": {"model": value}}
                before = copy.deepcopy(meta)
                checks = {check.name: check for check in validate_upload_meta(
                    meta, log_id=LOG_ID, duration_ms=1000)}
                self.assertEqual(checks["upload.meta.device"].status, "fail")
                self.assertEqual(meta, before)
        meta = {"logId": LOG_ID, "durationMs": 1000,
                "platform": {"os": "android"}, "device": {"model": "fixture-model"}}
        checks = validate_upload_meta(meta, log_id=LOG_ID, duration_ms=1000)
        self.assertFalse(any(check.status == "fail" for check in checks))
        self.assertNotIn("video", meta)
        self.assertNotIn("systemVersion", meta["device"])

    def test_sidecar_full_device_text_fields_reject_whitespace_without_filling(self):
        for key in ("model", "systemVersion"):
            with self.subTest(key=key):
                metadata = native_fixture()
                metadata["device"][key] = "  "
                checks = sidecar_checks(metadata)
                self.assertEqual(checks["device.android"].status, "fail")

    def test_missing_top_level_nested_fields_rejects_without_exception(self):
        for field in ("video", "session", "chunk", "source", "appVersion"):
            with self.subTest(field=field):
                metadata = native_fixture()
                metadata.pop(field)
                checks = self.assert_field_fails(metadata, field)
                self.assertEqual(checks["metadata_json.valid"].status, "fail")
