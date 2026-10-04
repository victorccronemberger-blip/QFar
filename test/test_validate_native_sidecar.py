"""Offline contract fixtures from APK 1.28 builders, not acquired device data.

Ll2/o0.c/j/l, Ll2/Z.c, Ll2/q.M and Ll2/K0.m prove the field/CSV formats.
Literal values below exercise those formats without running sensor generators,
media tools, remote clients or reading private state.
"""
from __future__ import annotations

import copy
import io
import json
import socket
import subprocess
import zipfile

import pytest

from moneymin.validate import summarize, validate_sidecar_zip, validate_upload_meta

LOG_ID = "native-session_0"
SENSOR_NS = 10_000_000_000_000
DURATION_MS = 1000

FRAMES = (
    "i,ptsNs,dtNs,tNs,key\n"
    f"0,10000000,0,{SENSOR_NS},1\n"
    f"1,343333333,333333333,{SENSOR_NS + 333400000},0\n"
    f"2,676666666,333333333,{SENSOR_NS + 666700000},0\n"
    f"3,1010000000,333333334,{SENSOR_NS + 1000000000},1\n"
)
IMU = (
    "t,ax,ay,az,wx,wy,wz\n"
    f"{SENSOR_NS - 1000000},0.02,-0.01,9.81,0.001,0.002,0.003\n"
    f"{SENSOR_NS + 333000000},0.03,-0.01,9.82,0.001,0.002,0.003\n"
    f"{SENSOR_NS + 667000000},0.02,-0.02,9.80,0.001,0.002,0.003\n"
    f"{SENSOR_NS + 1001000000},0.03,-0.01,9.81,0.001,0.002,0.003\n"
)


@pytest.fixture(autouse=True)
def _offline_only(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("validation tests cannot access network or processes")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


@pytest.fixture
def metadata():
    return {
        "id": LOG_ID, "logId": LOG_ID, "createdAt": "2026-10-03T00:00:00.000Z",
        "durationMs": DURATION_MS, "appVersion": "1.28.0",
        "platform": {"os": "android", "version": 34},
        "device": {"model": "fixture-model", "systemName": "Android", "systemVersion": "14"},
        "video": {"path": "/fixture/native-session_0.mp4", "width": 1440, "height": 1080, "rotationDeg": 90},
        "session": {"id": "native-session"},
        "chunk": {"index": 0, "startTimeMs": 1790985600000, "endTimeMs": 1790985601000},
        "source": "ego",
        "timebase": {
            "clockDomain": "android_elapsedRealtimeNanos",
            "startNs": str(SENSOR_NS - 10000000), "endNs": str(SENSOR_NS + 1020000000),
            "startWallTimeMs": 1790985600000, "endWallTimeMs": 1790985601000,
            "startSensorTimestampNs": str(SENSOR_NS - 1000000),
            "endSensorTimestampNs": str(SENSOR_NS + 1001000000),
            "firstFrameSensorTimestampNs": str(SENSOR_NS),
        },
        "cameras": [{
            "name": "camera_logical_0", "source": "builtin",
            "extrinsics_omitted_reason": "no_camera_imu_calibration",
            "intrinsics": {
                "fx": 1000.0, "fy": 1000.0, "cx": 720.0, "cy": 540.0,
                "width": 1440, "height": 1080, "coordinate_frame": "video_frame",
                "intrinsics_reference_dimensions": {"width": 1440, "height": 1080},
                "distortion_model": "pinhole", "distortion_coefficients": [],
            },
        }],
        "imuDiagnostics": {
            "strategy": "gyro_anchored_v1", "sampleCount": 4,
            "interpolatedCount": 2, "nearestFallbackCount": 2, "droppedRowCount": 0,
            "nearestFallbackToleranceNs": "1000000", "maxInterpolationSpanNs": "25000000",
            "maxAlignmentDeltaNs": "500000", "p95AlignmentDeltaNs": "400000",
        },
        "artifacts": [
            {"name": "imu", "remoteFilename": LOG_ID + ".imu.csv", "contentType": "text/csv"},
            {"name": "frames", "remoteFilename": LOG_ID + ".frames.csv", "contentType": "text/csv"},
        ],
        "codecActuals": {"mime": "video/avc", "width": 1440, "height": 1080,
                         "profile": 8, "level": 8192, "hasBFrames": None, "gopMaxFrames": None},
    }


def bundle(metadata, *, frames=FRAMES, imu=IMU, extra=()):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(LOG_ID + ".metadata.json", json.dumps(metadata))
        archive.writestr(LOG_ID + ".imu.csv", imu)
        archive.writestr(LOG_ID + ".frames.csv", frames)
        for name, value in extra:
            archive.writestr(name, value)
    return output.getvalue()


def checks(metadata, *, frames=FRAMES, imu=IMU):
    result = validate_sidecar_zip(bundle(metadata, frames=frames, imu=imu),
                                  log_id=LOG_ID, duration_ms=DURATION_MS)
    return {check.name: check for check in result}


def test_native_os_nonzero_pts_and_sensor_jitter_are_supported(metadata):
    result = checks(metadata)
    assert result["platform.android"].status == "pass"
    assert result["xcheck.frames_timebase"].status == "pass"
    assert not [check for check in result.values() if check.status == "fail"]
    # Sparse literal sensor rows are explicitly unrepresentative of sampling rate.
    assert result["imu.rate"].status == "warn"


def test_legacy_type_is_explicitly_qualified(metadata):
    metadata["platform"] = {"type": "android", "version": 34}
    result = checks(metadata)
    assert result["platform.android"].status == "warn"
    assert "legado" in result["platform.android"].detail


@pytest.mark.parametrize("platform", [
    {"os": "android", "type": "ios", "version": 34},
    {"os": "ios", "type": "android", "version": 34},
    {"os": "android", "version": True},
    {"os": "android", "version": "34"},
    {"os": "android", "version": 0},
    {"os": None, "type": "android", "version": 34},
])
def test_conflicting_or_invalid_platform_is_not_silently_normalized(metadata, platform):
    metadata["platform"] = platform
    assert checks(metadata)["platform.android"].status == "fail"


def test_matching_dual_platform_keys_are_accepted(metadata):
    metadata["platform"]["type"] = "android"
    assert checks(metadata)["platform.android"].status == "pass"


def external(metadata):
    metadata = copy.deepcopy(metadata)
    camera_anchor = 1_000_000
    tb = metadata["timebase"]
    tb.update(clockDomain="trinet_camera_monotonic", startNs=str(camera_anchor),
              endNs=str(camera_anchor + 1_000_000_000), firstFrameSensorTimestampNs=str(camera_anchor),
              hostStartElapsedNs=str(SENSOR_NS - 10_000_000),
              hostEndElapsedNs=str(SENSOR_NS + 1_020_000_000), frameTimestampSemantics="mid_exposure")
    frames = FRAMES
    for offset in (0, 333400000, 666700000, 1000000000):
        frames = frames.replace(str(SENSOR_NS + offset), str(camera_anchor + offset))
    return metadata, frames


def test_external_camera_and_host_clocks_are_not_compared_as_same_domain(metadata):
    metadata, frames = external(metadata)
    result = checks(metadata, frames=frames)
    assert result["timebase.external_host"].status == "pass"
    assert result["xcheck.frames_timebase"].status == "pass"
    assert not [check for check in result.values() if check.status == "fail"]


def test_external_missing_host_anchors_stays_unconfirmed(metadata):
    metadata, frames = external(metadata)
    metadata["timebase"].pop("hostStartElapsedNs")
    metadata["timebase"].pop("hostEndElapsedNs")
    assert checks(metadata, frames=frames)["timebase.external_host"].status == "warn"


@pytest.mark.parametrize("field,value", [
    ("hostStartElapsedNs", "bad"), ("hostEndElapsedNs", None),
    ("hostEndElapsedNs", "1"),
])
def test_external_bad_host_pair_fails_without_touching_camera_clock(metadata, field, value):
    metadata, frames = external(metadata)
    metadata["timebase"][field] = value
    result = checks(metadata, frames=frames)
    assert result["timebase.external_host"].status == "fail"
    assert result["xcheck.frames_timebase"].status == "pass"


def test_external_unknown_semantics_is_a_warning(metadata):
    metadata, frames = external(metadata)
    metadata["timebase"]["frameTimestampSemantics"] = "unknown"
    assert checks(metadata, frames=frames)["timebase.external_host"].status == "warn"


def test_zero_anchor_does_not_claim_clock_correlation(metadata):
    metadata["timebase"]["firstFrameSensorTimestampNs"] = "0"
    assert checks(metadata)["xcheck.frames_timebase"].status == "warn"


def test_positive_anchor_difference_is_unconfirmed_without_rejecting_capture(metadata):
    metadata["timebase"]["firstFrameSensorTimestampNs"] = str(SENSOR_NS + 1)
    result = checks(metadata)
    assert result["xcheck.frames_timebase"].status == "warn"
    assert result["frames.csv"].status == "pass"
    assert not any(check.status == "fail" for check in result.values())


def test_zero_based_csv_cannot_impersonate_elapsed_capture_anchor(metadata):
    frames = FRAMES.replace(str(SENSOR_NS), "10000000", 1)
    result = checks(metadata, frames=frames)
    assert result["xcheck.frames_timebase"].status == "warn"
    assert result["xcheck.duration_consistency.frame_timestamps"].status == "fail"


def test_host_and_frame_intervals_must_overlap(metadata):
    metadata["timebase"].update(startNs="1", endNs="2000000000")
    assert checks(metadata)["xcheck.frames_timebase"].status == "fail"


def test_unknown_clock_is_explicitly_unconfirmed(metadata):
    metadata["timebase"]["clockDomain"] = "unrecognized_clock"
    result = checks(metadata)
    assert result["timebase.clockDomain"].status == "warn"
    assert result["xcheck.frames_timebase"].status == "warn"


def test_frame_delta_must_match_pts_even_when_sensor_clock_has_jitter(metadata):
    frames = FRAMES.replace("1,343333333,333333333,", "1,343333333,7,")
    assert checks(metadata, frames=frames)["frames.csv"].status == "fail"


def test_sensor_frame_span_cannot_hide_behind_consistent_pts(metadata):
    frames = FRAMES.replace(str(SENSOR_NS + 1000000000), str(SENSOR_NS + 60000000000))
    assert checks(metadata, frames=frames)["xcheck.duration_consistency.frame_timestamps"].status == "fail"


def test_imu_timestamps_keep_integer_precision_above_float_range():
    from moneymin.validate import _check_imu_csv

    start = 2**53 + 2
    csv = "t,ax,ay,az,wx,wy,wz\n" + "".join(
        f"{start + offset},0,0,9.81,0,0,0\n" for offset in range(3))
    # A float conversion collapsed the last two timestamps and falsely failed.
    result = {check.name: check for check in _check_imu_csv(csv, 1)}
    assert result["imu.csv"].status == "pass"
    assert result["imu.rate"].status == "warn"


@pytest.mark.parametrize("timestamp", ["NaN", "Infinity", "1.5", "-1", str(2**63)])
def test_invalid_imu_timestamp_returns_failure_without_exception(metadata, timestamp):
    imu = IMU.replace(str(SENSOR_NS - 1000000), timestamp, 1)
    assert checks(metadata, imu=imu)["imu.csv"].status == "fail"


@pytest.mark.parametrize("field,value", [
    ("timebase", ["not", "an", "object"]), ("cameras", [7]),
    ("cameras", [{"name": "camera_logical_0", "intrinsics": []}]),
    ("codecActuals", "not an object"), ("artifacts", 42),
])
def test_malformed_nested_objects_return_diagnostics_not_tracebacks(metadata, field, value):
    metadata[field] = value
    assert any(check.status == "fail" for check in checks(metadata).values())


@pytest.mark.parametrize("value", ["not-a-number", "-1", "", None, True, str(2**63)])
def test_malformed_ns_metadata_does_not_raise(metadata, value):
    metadata["timebase"]["endNs"] = value
    result = checks(metadata)
    assert result["timebase.endNs"].status == "fail"
    assert result["timebase.span"].status == "fail"


@pytest.mark.parametrize("value", ["NaN", True, None, 0, 1.5])
def test_bad_duration_is_not_coerced(metadata, value):
    metadata["durationMs"] = value
    assert checks(metadata)["xcheck.duration_consistency.metadata"].status == "fail"


def test_both_native_identifiers_must_match_archive_prefix(metadata):
    metadata["id"] = "another-session_0"
    assert checks(metadata)["xcheck.metadata_json_matches_upload"].status == "fail"


def test_artifact_descriptor_must_identify_its_zip_member(metadata):
    metadata["artifacts"][0]["remoteFilename"] = "another-session_0.imu.csv"
    assert checks(metadata)["artifact.metadata_json_fields"].status == "fail"


@pytest.mark.parametrize("camera", [None, [{"name": "camera_logical_0", "source": "builtin",
                                        "intrinsics_omitted_reason": "no_calibrated_intrinsics_reported"}]])
def test_native_unavailable_calibration_is_reported_as_unverified(metadata, camera):
    if camera is None:
        metadata.pop("cameras")
    else:
        metadata["cameras"] = camera
    assert checks(metadata)["artifact.cameras_schema"].status == "warn"


def test_nonfinite_or_oversized_intrinsics_does_not_raise(metadata):
    metadata["cameras"][0]["intrinsics"]["fx"] = 10**400
    assert checks(metadata)["artifact.cameras_schema"].status == "fail"


@pytest.mark.parametrize("codec", [{}, {"mime": "video/avc", "width": None, "height": None}])
def test_native_unavailable_codec_values_are_unverified(metadata, codec):
    metadata["codecActuals"] = codec
    assert checks(metadata)["codecActuals.schema"].status == "warn"


def test_other_native_video_codec_is_not_assumed_to_be_avc(metadata):
    metadata["codecActuals"]["mime"] = "video/hevc"
    assert checks(metadata)["codecActuals.schema"].status == "pass"


@pytest.mark.parametrize("name,value", [("../unexpected", b"x"), ("hidden/file", b"x")])
def test_unexpected_zip_paths_are_rejected(metadata, name, value):
    result = validate_sidecar_zip(bundle(metadata, extra=[(name, value)]),
                                  log_id=LOG_ID, duration_ms=DURATION_MS)
    assert result[0].name == "zip" and result[0].status == "fail"


def test_bad_utf8_is_not_silently_replaced(metadata):
    result = validate_sidecar_zip(bundle(metadata, imu=b"t,ax,ay,az,wx,wy,wz\n\xff"),
                                  log_id=LOG_ID, duration_ms=DURATION_MS)
    assert result[0].name == "zip" and result[0].status == "fail"


def test_native_zip_missing_file_still_fails_local_completeness_check(metadata):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(LOG_ID + ".metadata.json", json.dumps(metadata))
        archive.writestr(LOG_ID + ".imu.csv", IMU)
    result = validate_sidecar_zip(output.getvalue(), log_id=LOG_ID, duration_ms=DURATION_MS)
    assert summarize(result)["counts"]["fail"] == 1
    assert "frames.csv" in result[0].detail


@pytest.mark.parametrize("duration", ["NaN", None, True, 1.5])
def test_malformed_upload_duration_is_diagnostic_not_exception(duration):
    meta = {"logId": LOG_ID, "durationMs": duration,
            "platform": {"os": "android"}, "device": {"model": "fixture-model"}}
    result = {check.name: check for check in validate_upload_meta(meta, log_id=LOG_ID, duration_ms=DURATION_MS)}
    assert result["upload.meta.duration"].status == "fail"


def test_nonobject_upload_meta_is_diagnostic():
    result = validate_upload_meta([], log_id=LOG_ID, duration_ms=DURATION_MS)
    assert result[0].name == "upload.meta.valid" and result[0].status == "fail"
