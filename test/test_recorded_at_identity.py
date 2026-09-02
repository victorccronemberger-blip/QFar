from __future__ import annotations

import re
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from moneymin import config, device_profile, minute_api, upload
from moneymin import campaign
from moneymin.campaign import _chunk_plan, _slice_imu_csv
from moneymin.device_profile import DeviceProfile
from moneymin.recording_timeline import RecordingSlot
from moneymin.sidecar import build_metadata_json
from moneymin.upload import (
    UploadError,
    _normalize_recorded_at_sequence,
    _recorded_at_sequence_from_base,
)


_ISO_MS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


class RecordedAtTests(unittest.TestCase):
    def test_format_matches_js_toisostring(self) -> None:
        stamp = device_profile.format_recorded_at(1_785_184_974.562)
        self.assertRegex(stamp, _ISO_MS)
        self.assertTrue(stamp.endswith(".562Z"))
        self.assertEqual(len(stamp.split(".")[-1]), 4)  # 562Z

    def test_normalize_collapses_six_digits(self) -> None:
        out = device_profile.normalize_recorded_at(
            "2026-07-27T20:42:54.609000Z")
        self.assertRegex(out, _ISO_MS)
        self.assertTrue(out.endswith(".609Z"))

    def test_start_stays_inside_backlog(self) -> None:
        now = 1_800_000_000.0
        start = device_profile.recording_start_epoch(
            600.0, now=now, gap_s=20_000.0)
        self.assertGreaterEqual(
            start, now - device_profile.BACKLOG_CAP_MS / 1000.0)
        self.assertLessEqual(start, now - 600.0)
        stamp = device_profile.format_recorded_at(start)
        wall = device_profile.recorded_at_to_wall_ms(stamp)
        self.assertIsNotNone(wall)
        age_ms = now * 1000 - wall
        self.assertLess(age_ms, device_profile.BACKLOG_CAP_MS)

    def test_timeline_uses_three_digits(self) -> None:
        slot = RecordingSlot("a@b.c", 1_785_184_974.562, 1_785_185_074.562)
        self.assertRegex(slot.recorded_at, _ISO_MS)

    def test_chunk_sequence_preserves_eight_minute_variation(self) -> None:
        now = 1_800_000_000.0
        values = [
            device_profile.format_recorded_at(now - 1_000),
            device_profile.format_recorded_at(now - 520),
        ]

        normalized = _normalize_recorded_at_sequence(
            values, [480_000, 480_000], now=now)

        first = device_profile.recorded_at_to_wall_ms(normalized[0])
        second = device_profile.recorded_at_to_wall_ms(normalized[1])
        assert first is not None and second is not None
        self.assertEqual(second - first, 480_000)

    def test_chunk_sequence_rejects_collapsed_timestamps(self) -> None:
        now = 1_800_000_000.0
        value = device_profile.format_recorded_at(now - 1_000)
        with self.assertRaises(UploadError):
            _normalize_recorded_at_sequence(
                [value, value], [480_000, 480_000], now=now)

    def test_multi_chunk_base_expands_to_continuous_start_times(self) -> None:
        now = 1_800_000_000.0
        base = device_profile.format_recorded_at(now - 1_000)
        sequence = _recorded_at_sequence_from_base(
            base, [480_000, 480_000], now=now)
        first = device_profile.recorded_at_to_wall_ms(sequence[0])
        second = device_profile.recorded_at_to_wall_ms(sequence[1])
        assert first is not None and second is not None
        self.assertEqual(second - first, 480_000)


class IdentityConsistencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = DeviceProfile(
            email="id-test@example.com",
            device_id="11111111-2222-3333-4444-555555555555",
            device_model="iPhone 15",
            sidecar_model="iPhone15,4",
            os_version="26.5.2",
            sidecar_system_version="26.5",
            boot_wall_ms=1_785_000_000_000,
            created_wall_ms=1_785_100_000_000,
            clock_offset_ns=-126,
            frames_gop=30,
            video_bitrate_mbps=8.1,
        )

    def test_ua_version_and_payload_share_one_profile(self) -> None:
        ua = self.profile.user_agent()
        headers = self.profile.headers()
        opened = self.profile.opened_payload()
        self.assertEqual(
            ua,
            f"Minute/{config.APP_VERSION} "
            "(com.bakerdata.minute; build:1; iOS 26.5.2)",
        )
        self.assertEqual(headers["X-App-Version"], config.APP_VERSION)
        self.assertEqual(headers["User-Agent"], ua)
        self.assertEqual(headers["X-Device-Id"], self.profile.device_id)
        self.assertEqual(opened["app_version"], config.APP_VERSION)
        self.assertEqual(opened["device_model"], "iPhone 15")
        self.assertEqual(opened["os_version"], "ios 26.5.2")
        self.assertEqual(
            self.profile.upload_device_meta()["model"], "iPhone 15")
        self.assertEqual(
            self.profile.sidecar_device_meta()["model"], "iPhone15,4")
        self.assertEqual(
            self.profile.sidecar_device_meta()["systemVersion"], "26.5")
        self.assertEqual(
            self.profile.sidecar_platform_meta()["version"], "26.5")
        self.assertEqual(
            self.profile.sidecar_platform_meta()["os"], "ios")

    def test_sidecar_timebase_uses_profile_uptime_and_app_version(self) -> None:
        recorded_at = "2026-07-27T20:42:54.562Z"
        wall_ms = device_profile.recorded_at_to_wall_ms(recorded_at)
        assert wall_ms is not None
        meta = build_metadata_json(
            session_id="627d39d8-60e9-412d-8aea-a84af997c9a7",
            chunk_index=0,
            duration_ms=60_000,
            recorded_at=recorded_at,
            device_meta=self.profile.sidecar_device_meta(),
            platform_meta=self.profile.sidecar_platform_meta(),
            calib=self.profile.calib,
            clock_offset_ns=self.profile.clock_offset_ns,
            uptime_ns=self.profile.uptime_ns_at(wall_ms),
        )
        self.assertEqual(meta["appVersion"], config.APP_VERSION)
        self.assertEqual(meta["createdAt"], recorded_at)
        self.assertEqual(meta["device"]["model"], "iPhone15,4")
        self.assertEqual(meta["device"]["systemVersion"], "26.5")
        self.assertEqual(meta["platform"]["os"], "ios")
        self.assertEqual(meta["platform"]["version"], "26.5")
        self.assertEqual(meta["timebase"]["clockDomain"], "ios_systemUptimeNs")
        start_ns = int(meta["timebase"]["startNs"])
        self.assertEqual(start_ns, self.profile.uptime_ns_at(wall_ms))
        self.assertNotEqual(start_ns, 224_584_000_000_000)
        self.assertEqual(
            meta["timebase"]["startWallTimeMs"], wall_ms)

    def test_location_header_only_when_coords_exist(self) -> None:
        self.assertNotIn("X-Device-Location", self.profile.headers())
        self.profile.latitude = -23.5505
        self.profile.longitude = -46.6333
        self.profile.location_accuracy = 10.0
        value = self.profile.location_header_value()
        self.assertIsNotNone(value)
        self.assertIn("latitude", value or "")
        self.assertEqual(
            self.profile.headers()["X-Device-Location"], value)

    def test_invalid_location_is_never_sent(self) -> None:
        self.profile.latitude = float("nan")
        self.profile.longitude = -46.6333
        self.assertIsNone(self.profile.location_header_value())
        self.profile.latitude = 91.0
        self.assertNotIn("X-Device-Location", self.profile.headers())

    def test_warmup_runs_once_per_account_across_sessions(self) -> None:
        email = "warmup-once@example.com"
        minute_api._WARMED_IDENTITIES.discard(email)
        first = minute_api.Session({"idToken": "token", "email": email}, email=email)
        second = minute_api.Session({"idToken": "token", "email": email}, email=email)
        try:
            with (
                mock.patch.object(
                    minute_api.Session, "app_opened", return_value=(202, "")) as opened,
                mock.patch.object(
                    minute_api.Session, "fetch_recording_config",
                    return_value=(200, '{"backlogCapMs":14400000}')),
            ):
                first.warmup()
                second.warmup()
            self.assertEqual(opened.call_count, 1)
        finally:
            minute_api._WARMED_IDENTITIES.discard(email)


class ChunkPlanTests(unittest.TestCase):
    def test_short_recording_is_one_chunk(self) -> None:
        self.assertEqual(_chunk_plan(96_100), [(0, 96_100)])

    def test_long_recording_splits_without_tiny_tail(self) -> None:
        plan = _chunk_plan(960_000)
        self.assertEqual(plan, [(0, 480_000), (480_000, 480_000)])
        self.assertEqual(sum(dur for _, dur in plan), 960_000)
        self.assertTrue(all(dur >= 60_000 for _, dur in plan))

    def test_imu_slice_rebases_timestamps(self) -> None:
        csv = "t,ax,ay,az,wx,wy,wz\n0,1,0,0,0,0,0\n10000000,1,0,0,0,0,0\n"
        sliced, n = _slice_imu_csv(csv, 10, 10)
        self.assertEqual(n, 1)
        self.assertIn("\n0,1,0,0,0,0,0", sliced)

    def test_parallel_chunk_cut_is_built_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qmoney-chunk-test-") as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            destination = root / "source_ch0.mp4"
            source.write_bytes(b"source")
            calls = 0
            calls_lock = threading.Lock()

            def fake_run(command: list[str]) -> SimpleNamespace:
                nonlocal calls
                with calls_lock:
                    calls += 1
                time.sleep(0.02)
                Path(command[-1]).write_bytes(b"complete chunk")
                return SimpleNamespace(returncode=0, stderr="")

            with (
                mock.patch.object(campaign, "_ffmpeg_run", side_effect=fake_run),
                mock.patch.object(
                    campaign, "probe_video", return_value={"duration_ms": 60_000}),
            ):
                with ThreadPoolExecutor(max_workers=6) as pool:
                    results = list(pool.map(
                        lambda _: campaign._cut_video_chunk(
                            source, destination, 0.0, 60.0),
                        range(6),
                    ))

            self.assertEqual(calls, 1)
            self.assertTrue(all(path == destination for path in results))
            self.assertEqual(destination.read_bytes(), b"complete chunk")

    def test_legacy_account_encode_is_not_recertified(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qmoney-account-cache-test-") as tmp:
            root = Path(tmp)
            base = root / "base.mp4"
            encoded = root / "base_acclegacy.mp4"
            base.write_bytes(b"base")
            encoded.write_bytes(b"legacy")
            now = time.time()
            os.utime(base, (now - 20, now - 20))
            os.utime(encoded, (now - 10, now - 10))
            marker = campaign._account_video_ok_path(encoded)
            marker.write_text("ok", encoding="utf-8")
            os.utime(marker, (now, now))

            with mock.patch.object(campaign, "probe_video") as probe:
                self.assertFalse(campaign._valid_account_video(encoded, base))

            probe.assert_not_called()
            self.assertEqual(marker.read_text(encoding="utf-8"), "ok")


class UploadContractTests(unittest.TestCase):
    def test_post_and_sidecar_keep_each_chunk_recorded_at(self) -> None:
        class FakeSession:
            def __init__(self) -> None:
                self.upload_bodies: list[dict] = []

            def request(self, method: str, path: str, body=None):
                if path == "/api/v1/storage/sas/blobs":
                    return 200, json.dumps({
                        "signed_urls": [
                            {
                                "filename": item["filename"],
                                "blob_url": f"https://blob.invalid/{item['filename']}",
                            }
                            for item in body["files"]
                        ],
                    })
                if path.startswith("/api/v1/uploads?"):
                    self.upload_bodies.append(body)
                    return 201, json.dumps({"id": f"upload-{len(self.upload_bodies)}"})
                if path.endswith("/complete"):
                    return 200, "{}"
                if path.endswith("/finalize"):
                    return 204, ""
                raise AssertionError(f"chamada inesperada: {method} {path}")

        with tempfile.TemporaryDirectory(prefix="qmoney-upload-contract-") as tmp:
            root = Path(tmp)
            paths = [root / "chunk0.mp4", root / "chunk1.mp4"]
            for path in paths:
                path.write_bytes(b"video")
            now = time.time()
            recorded = [
                device_profile.format_recorded_at(now - 180),
                device_profile.format_recorded_at(now - 120),
            ]
            session = FakeSession()
            probe = {
                "duration_ms": 60_000,
                "fps": 30.0,
                "width": 1440,
                "height": 1080,
                "codec": "h264",
                "profile": "High",
                "has_video": True,
                "has_audio": True,
            }
            with (
                mock.patch.object(upload, "_probe_duration_ms", return_value=60_000),
                mock.patch.object(upload, "probe_video", return_value=probe),
                mock.patch.object(upload, "_put_blob_file", return_value=201),
                mock.patch.object(upload, "_put_blob", return_value=201),
            ):
                result = upload.upload_session(
                    session, paths, "org", task_id="task",
                    recorded_at=recorded, normalize=False, register_first=True,
                    finalize=True, max_retries=1,
                )

        self.assertTrue(result.finalized)
        self.assertEqual(
            [body["recorded_at"] for body in session.upload_bodies], recorded)
        self.assertEqual(
            [body["meta"]["createdAt"] for body in session.upload_bodies],
            recorded,
        )


if __name__ == "__main__":
    unittest.main()
