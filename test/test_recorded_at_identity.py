from __future__ import annotations

import io
import json
import os
import re
import tempfile
import threading
import time
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from moneymin import config, device_profile, minute_api, upload
from moneymin import campaign, sidecar
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
            device_id="android.ssaid:0123456789abcdef",
            device_model="SM-S918B",
            sidecar_model="SM-S918B",
            os_version="14",
            sdk_int=34,
            sidecar_system_version="14",
            logical_camera_id="4",
            boot_wall_ms=1_785_000_000_000,
            created_wall_ms=1_785_100_000_000,
            frames_gop=30,
            video_bitrate_mbps=8.1,
        )

    def test_ua_version_and_payload_share_one_profile(self) -> None:
        ua = self.profile.user_agent()
        headers = self.profile.headers()
        opened = self.profile.opened_payload()
        # UA OkHttp do APK (4.12.0) — constante em todos os aparelhos.
        self.assertEqual(config.USER_AGENT, "okhttp/4.12.0")
        self.assertEqual(ua, config.USER_AGENT)
        self.assertEqual(headers["X-App-Version"], config.APP_VERSION)
        self.assertEqual(headers["User-Agent"], ua)
        self.assertEqual(headers["X-Device-Id"], "android.ssaid:0123456789abcdef")
        self.assertEqual(opened["app_version"], config.APP_VERSION)
        self.assertEqual(opened["device_model"], "SM-S918B")
        self.assertEqual(opened["os_version"], "android 14")
        self.assertEqual(
            self.profile.upload_device_meta()["model"], "SM-S918B")
        self.assertEqual(
            self.profile.sidecar_device_meta()["model"], "SM-S918B")
        self.assertEqual(
            self.profile.sidecar_device_meta()["systemVersion"], "14")
        self.assertEqual(
            self.profile.sidecar_platform_meta()["version"], 34)
        self.assertEqual(
            self.profile.sidecar_platform_meta()["type"], "android")

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
            uptime_ns=self.profile.uptime_ns_at(wall_ms),
        )
        self.assertEqual(meta["appVersion"], config.APP_VERSION)
        self.assertEqual(meta["createdAt"], recorded_at)
        self.assertEqual(meta["device"]["model"], "SM-S918B")
        self.assertEqual(meta["device"]["systemVersion"], "14")
        self.assertEqual(meta["platform"]["type"], "android")
        self.assertEqual(meta["platform"]["version"], 34)
        self.assertEqual(
            meta["timebase"]["clockDomain"], "android_elapsedRealtimeNanos")
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
        # CSV exato do formatDeviceLocationHeader do bundle Android (isMock minúsculo).
        self.assertEqual(value, "-23.550500,-46.633300,10,false")
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

    def test_remote_max_never_overflows_when_tail_is_small(self) -> None:
        limits = {
            "min_duration_ms": 60_000,
            "max_duration_ms": 300_000,
            "backlog_cap_ms": 14_400_000,
        }
        with mock.patch.dict(config._EFFECTIVE_LIMITS, limits, clear=True):
            for total in (350_000, 610_000):
                plan = _chunk_plan(total)
                self.assertEqual(sum(dur for _, dur in plan), total)
                self.assertTrue(all(
                    60_000 <= dur <= 300_000 for _, dur in plan))

    def test_duration_below_remote_minimum_is_rejected(self) -> None:
        limits = {
            "min_duration_ms": 120_000,
            "max_duration_ms": 300_000,
            "backlog_cap_ms": 14_400_000,
        }
        with mock.patch.dict(config._EFFECTIVE_LIMITS, limits, clear=True):
            with self.assertRaises(ValueError):
                _chunk_plan(90_000)

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
    def test_session_complete_always_suppresses_per_chunk_catbear(self) -> None:
        class FakeSession:
            def __init__(self) -> None:
                self.body: dict | None = None

            def request(self, method: str, path: str, body=None):
                self.body = body
                return 200, "{}"

        session = FakeSession()
        upload.complete_upload(
            session, "upload-1", 1234,
            suppress_per_chunk_catbear=False,
            session_complete=True,
        )

        self.assertIsNotNone(session.body)
        self.assertIs(session.body["session_complete"], True)
        self.assertIs(session.body["suppress_per_chunk_catbear"], True)

    def test_finalize_flow_does_not_mix_session_complete(self) -> None:
        class FakeSession:
            def __init__(self) -> None:
                self.upload_bodies: list[dict] = []
                self.complete_bodies: list[dict] = []

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
                    self.complete_bodies.append(body)
                    return 200, "{}"
                if path.endswith("/finalize"):
                    return 204, ""
                raise AssertionError(f"chamada inesperada: {method} {path}")

        with tempfile.TemporaryDirectory(prefix="qmoney-upload-contract-") as tmp:
            root = Path(tmp)
            paths = [root / "c0.mp4", root / "c1.mp4"]
            for path in paths:
                path.write_bytes(b"v")
            now = time.time()
            recorded = [
                device_profile.format_recorded_at(now - 180),
                device_profile.format_recorded_at(now - 120),
            ]
            session = FakeSession()
            probe = {
                "duration_ms": 60_000, "fps": 30.0, "width": 1440,
                "height": 1080, "codec": "h264", "has_video": True,
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
                    finalize=True, max_retries=1, sidecar=False,
                    suppress_per_chunk_catbear=True,
                )

        self.assertTrue(result.finalized)
        self.assertEqual(len(session.complete_bodies), 2)
        # O QMoney usa o endpoint explícito /finalize. O sinal migratório do
        # Android não pode ser combinado com ele: essa combinação prende a
        # geração de previews no backend atual.
        self.assertNotIn("session_complete", session.complete_bodies[0])
        self.assertNotIn("session_complete", session.complete_bodies[1])
        self.assertIs(
            session.complete_bodies[0]["suppress_per_chunk_catbear"], True)
        self.assertIs(
            session.complete_bodies[1]["suppress_per_chunk_catbear"], True)

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


class SensorFidelityTests(unittest.TestCase):
    def test_imu_csv_is_500hz_and_span_matches_duration(self) -> None:
        text = sidecar.build_imu_csv(60_000, seed="sensor-t")
        rows = text.strip().splitlines()
        self.assertEqual(rows[0], "t,ax,ay,az,wx,wy,wz")
        self.assertEqual(len(rows) - 1, 30_001)  # 500 Hz * 60 s + 1
        t0 = int(rows[1].split(",")[0])
        t1 = int(rows[2].split(",")[0])
        self.assertEqual(t1 - t0, 2_000_000)  # 2 ms em ns
        self.assertEqual(int(rows[-1].split(",")[0]), 60_000 * 1_000_000)

    def test_imu_signal_differs_per_account(self) -> None:
        a = sidecar.build_imu_csv(6_000, seed="conta-a")
        b = sidecar.build_imu_csv(6_000, seed="conta-b")
        self.assertNotEqual(a, b)

    def test_imu_az_is_positive_gravity_android(self) -> None:
        rows = sidecar.build_imu_csv(2_000, seed="g").strip().splitlines()[1:]
        azs = [float(r.split(",")[3]) for r in rows[50:-50]]
        self.assertGreater(sum(azs) / len(azs), 9.0)

    def test_imu_gravity_magnitude_is_one_g(self) -> None:
        # Regressão: o gerador não pode somar gravidade 2x (|g| ≈ 19.6 era bug).
        import math as _math
        rows = sidecar.build_imu_csv(4_000, seed="g2").strip().splitlines()[1:]
        magnitudes = []
        for raw in rows[50:-50]:
            values = [float(v) for v in raw.split(",")]
            ax, ay, az = values[1], values[2], values[3]
            magnitudes.append(_math.sqrt(ax * ax + ay * ay + az * az))
        mean_g = sum(magnitudes) / len(magnitudes)
        self.assertGreater(mean_g, 9.3)
        self.assertLess(mean_g, 10.3)

    def test_frames_from_video_falls_back_to_synthetic(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="qmoney-frames-video-") as tmp:
            video = Path(tmp) / "clip.mp4"
            video.write_bytes(b"not a real mp4")
            text = sidecar.build_frames_csv_from_video(
                video, duration_ms=10_000, offset_ns=1_234_000_000)
        rows = text.strip().splitlines()
        self.assertEqual(rows[0], "i,ptsNs,dtNs,tNs,key")
        self.assertEqual(len(rows) - 1, 301)  # 30 fps * 10 s + 1
        pts = [int(r.split(",")[1]) for r in rows[1:]]
        self.assertTrue(all(later >= earlier
                            for earlier, later in zip(pts, pts[1:])))
        self.assertEqual(int(rows[1].split(",")[3]), 1_234_000_000)

    def test_frames_from_real_mp4_uses_actual_pts_and_keyframes(self) -> None:
        ffmpeg = sidecar.ffmpeg_bin()
        if not ffmpeg or ffmpeg == "ffmpeg":
            self.skipTest("ffmpeg indisponível para gerar MP4 de teste")
        import subprocess as _subprocess

        raw: list[tuple[int, bool]] = []
        with tempfile.TemporaryDirectory(prefix="qmoney-frames-real-") as tmp:
            video = Path(tmp) / "real.mp4"
            try:
                proc = _subprocess.run([
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i",
                    "testsrc=duration=2:size=160x120:rate=30",
                    "-c:v", "libx264", "-preset", "ultrafast",
                    "-g", "6", "-keyint_min", "6", "-pix_fmt", "yuv420p",
                    "-r", "30", str(video),
                ], capture_output=True, text=True, timeout=120)
            except Exception:
                proc = None
            if proc is None or proc.returncode != 0 or not video.exists():
                self.skipTest("ffmpeg não conseguiu gerar o MP4")
            raw = sidecar._extract_frame_pts_mp4(video)
        self.assertEqual(len(raw), 60)  # 2 s × 30 fps
        keys = [i for i, (_pts, key) in enumerate(raw) if key]
        self.assertEqual(keys[:3], [0, 6, 12])
        self.assertEqual(raw[1][0], 33_333_333)

    def test_sidecar_zip_members_have_logid_prefix(self) -> None:
        data = sidecar.build_sidecar_zip(
            session_id="zip-session", chunk_index=0, duration_ms=30_000,
            recorded_at="2026-08-20T12:00:00.000Z",
            calib={
                "fx": 1545, "fy": 1543, "cx": 2016, "cy": 1512,
                "referenceWidth": 4032, "referenceHeight": 3024,
                "k1": -0.24, "k2": 0.12, "k3": -0.035,
                "p1": 0.001, "p2": -0.002, "readoutS": 0.0104,
                "logicalCameraId": "4",
            },
            uptime_ns=88_000_000_000_000,
            device_meta={"model": "SM-S918B", "systemName": "Android",
                         "systemVersion": "14"},
            platform_meta={"type": "android", "version": 34},
            imu_seed="android.ssaid:0123456789abcdef",
        )
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            members = sorted(zf.namelist())
        self.assertEqual(
            members,
            ["zip-session_0.frames.csv", "zip-session_0.imu.csv",
             "zip-session_0.metadata.json"])


class ValidatorTests(unittest.TestCase):
    def _zip_bytes(self) -> bytes:
        return sidecar.build_sidecar_zip(
            session_id="val-session", chunk_index=0, duration_ms=60_000,
            recorded_at="2026-08-20T12:00:00.000Z",
            calib={
                "fx": 1545, "fy": 1543, "cx": 2016, "cy": 1512,
                "referenceWidth": 4032, "referenceHeight": 3024,
                "k1": -0.24, "k2": 0.12, "k3": -0.035,
                "p1": 0.001, "p2": -0.002, "readoutS": 0.0104,
                "logicalCameraId": "4",
            },
            uptime_ns=88_000_000_000_000,
            device_meta={"model": "SM-S918B", "systemName": "Android",
                         "systemVersion": "14"},
            platform_meta={"type": "android", "version": 34},
            imu_seed="android.ssaid:aaaaaaaaaaaaaaaa",
        )

    def test_built_sidecar_passes_local_checklist(self) -> None:
        from moneymin.validate import summarize, validate_sidecar_zip
        summary = summarize(validate_sidecar_zip(
            self._zip_bytes(), log_id="val-session_0", duration_ms=60_000))
        self.assertEqual(summary["counts"]["fail"], 0)

    def test_corrupted_imu_header_is_caught(self) -> None:
        from moneymin.validate import summarize, validate_sidecar_zip
        payload = bytearray(self._zip_bytes())
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            inner = {name: zf.read(name) for name in zf.namelist()}
        inner["val-session_0.imu.csv"] = (
            b"t,ax,ay,az\n" + inner["val-session_0.imu.csv"].split(b"\n", 1)[1])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, content in inner.items():
                zf.writestr(name, content)
        summary = summarize(validate_sidecar_zip(
            buf.getvalue(), log_id="val-session_0", duration_ms=60_000))
        self.assertGreater(summary["counts"]["fail"], 0)


class PropertyAuditTests(unittest.TestCase):
    """Matriz reduzida do fuzz: bordas de duração × modelos × seeds, 0 fails."""

    _MODELS = [
        ("SM-G991B", "2", 1250.0),
        ("SM-S908B", "3", 1522.0),
        ("SM-S918B", "4", 1600.0),
        ("SM-S928B", "5", 1605.0),
    ]
    _DURATIONS = [60_000, 61_000, 480_000]
    _SEEDS = ["android.ssaid:0123456789abcdef"]

    def test_all_edge_sidecars_pass_the_local_checklist(self) -> None:
        from moneymin.validate import summarize, validate_sidecar_zip
        fails: list[str] = []
        # 1800s (máximo) só 1 caso — a malha completa é o scripts/fuzz_sidecars.py.
        for duration in self._DURATIONS + [1_800_000]:
            for model, cam_id, fx in self._MODELS:
                for seed in self._SEEDS:
                    if duration == 1_800_000 and (model, seed) != (
                            self._MODELS[0][0], self._SEEDS[0]):
                        continue
                    payload = sidecar.build_sidecar_zip(
                        session_id="prop", chunk_index=0,
                        duration_ms=duration,
                        recorded_at="2026-08-20T12:00:00.000Z",
                        calib={
                            "fx": fx, "fy": fx, "cx": 2016, "cy": 1512,
                            "referenceWidth": 4032, "referenceHeight": 3024,
                            "k1": -0.24, "k2": 0.12, "k3": -0.035,
                            "p1": 0.001, "p2": -0.002, "readoutS": 0.0105,
                            "logicalCameraId": cam_id,
                        },
                        uptime_ns=88_000_000_000_000,
                        device_meta={"model": model, "systemName": "Android",
                                     "systemVersion": "14"},
                        platform_meta={"type": "android", "version": 34},
                        imu_seed=seed,
                    )
                    summary = summarize(validate_sidecar_zip(
                        payload, log_id="prop_0", duration_ms=duration))
                    if summary["counts"]["fail"]:
                        fails.append(
                            f"{model} {duration}ms {seed}: "
                            + "; ".join(f"{f['id']}" for f in summary["failures"]))
        self.assertEqual(fails, [])

    def test_multi_chunk_slice_keeps_each_chunk_consistent(self) -> None:
        from moneymin.campaign import _slice_imu_csv
        from moneymin.validate import summarize, validate_sidecar_zip

        full = sidecar.build_imu_csv(960_000, seed="android.ssaid:abcdef")
        for index, (start_ms, dur_ms) in enumerate(((0, 480_000), (480_000, 480_000))):
            part, n = _slice_imu_csv(full, start_ms, dur_ms)
            payload = sidecar.build_sidecar_zip_custom(
                session_id="prop-mc", chunk_index=index, duration_ms=dur_ms,
                recorded_at="2026-08-20T12:00:00.000Z",
                calib={
                    "fx": 1600, "fy": 1600, "cx": 2016, "cy": 1512,
                    "referenceWidth": 4032, "referenceHeight": 3024,
                    "k1": -0.24, "k2": 0.12, "k3": -0.035,
                    "p1": 0.001, "p2": -0.002, "readoutS": 0.0112,
                    "logicalCameraId": "4",
                },
                uptime_ns=88_000_000_000_000,
                device_meta={"model": "SM-S918B", "systemName": "Android",
                             "systemVersion": "14"},
                platform_meta={"type": "android", "version": 34},
                imu_csv=part, frames_csv=sidecar.build_frames_csv(
                    dur_ms, fps=30.0, gop=30, offset_ns=88_000_000_000_000),
                imu_sample_count=n,
            )
            summary = summarize(validate_sidecar_zip(
                payload, log_id=f"prop-mc_{index}", duration_ms=dur_ms))
            self.assertEqual(summary["counts"]["fail"], 0,
                             f"chunk {index}: {summary['failures']}")


class ValidatorRegressionTests(unittest.TestCase):
    """Regressões dos falsos positivos do validador (P2 identificados)."""

    @staticmethod
    def _zip() -> bytes:
        return sidecar.build_sidecar_zip(
            session_id="val-session", chunk_index=0, duration_ms=60_000,
            recorded_at="2026-08-20T12:00:00.000Z",
            calib={
                "fx": 1545, "fy": 1543, "cx": 2016, "cy": 1512,
                "referenceWidth": 4032, "referenceHeight": 3024,
                "k1": -0.24, "k2": 0.12, "k3": -0.035,
                "p1": 0.001, "p2": -0.002, "readoutS": 0.0104,
                "logicalCameraId": "4",
            },
            uptime_ns=88_000_000_000_000,
            device_meta={"model": "SM-S918B", "systemName": "Android",
                         "systemVersion": "14"},
            platform_meta={"type": "android", "version": 34},
            imu_seed="android.ssaid:aaaaaaaaaaaaaaaa",
        )

    @staticmethod
    def _repack(members: dict[str, bytes]) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, content in members.items():
                zf.writestr(name, content)
        return buf.getvalue()

    @staticmethod
    def _inner(payload: bytes) -> dict[str, bytes]:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            return {name: zf.read(name) for name in zf.namelist()}

    def test_sample_count_mismatch_is_a_fail(self) -> None:
        from moneymin.validate import summarize, validate_sidecar_zip
        inner = self._inner(self._zip())
        meta = json.loads(inner["val-session_0.metadata.json"])
        meta["imuDiagnostics"]["sampleCount"] = 123  # falso positivo antigo
        inner["val-session_0.metadata.json"] = json.dumps(meta).encode()
        summary = summarize(validate_sidecar_zip(
            self._repack(inner), log_id="val-session_0", duration_ms=60_000))
        self.assertGreater(summary["counts"]["fail"], 0)
        self.assertIn(
            "imuDiagnostics.sampleCount",
            [f["id"] for f in summary["failures"]])

    def test_sample_count_off_by_one_is_a_fail(self) -> None:
        from moneymin.validate import summarize, validate_sidecar_zip
        inner = self._inner(self._zip())
        meta = json.loads(inner["val-session_0.metadata.json"])
        meta["imuDiagnostics"]["sampleCount"] += 1
        inner["val-session_0.metadata.json"] = json.dumps(meta).encode()
        summary = summarize(validate_sidecar_zip(
            self._repack(inner), log_id="val-session_0", duration_ms=60_000))
        self.assertIn(
            "imuDiagnostics.sampleCount",
            [f["id"] for f in summary["failures"]])

    def test_frame_clock_mismatch_is_a_fail(self) -> None:
        from moneymin.validate import summarize, validate_sidecar_zip
        inner = self._inner(self._zip())
        name = "val-session_0.frames.csv"
        lines = inner[name].decode().splitlines()
        first = lines[1].split(",")
        first[3] = first[1]  # remove uptime: tNs volta ao relógio zero-based
        lines[1] = ",".join(first)
        inner[name] = ("\n".join(lines) + "\n").encode()
        summary = summarize(validate_sidecar_zip(
            self._repack(inner), log_id="val-session_0", duration_ms=60_000))
        self.assertIn(
            "xcheck.frames_timebase",
            [f["id"] for f in summary["failures"]])

    def test_invalid_distortion_layout_is_a_fail(self) -> None:
        from moneymin.validate import summarize, validate_sidecar_zip
        inner = self._inner(self._zip())
        meta = json.loads(inner["val-session_0.metadata.json"])
        meta["cameras"][0]["intrinsics"][
            "distortion_coefficients_layout"] = "invalid"
        inner["val-session_0.metadata.json"] = json.dumps(meta).encode()
        summary = summarize(validate_sidecar_zip(
            self._repack(inner), log_id="val-session_0", duration_ms=60_000))
        self.assertIn(
            "artifact.cameras_schema",
            [f["id"] for f in summary["failures"]])

    def test_header_only_csv_reproves_not_crashes(self) -> None:
        from moneymin.validate import summarize, validate_sidecar_zip
        inner = self._inner(self._zip())
        inner["val-session_0.imu.csv"] = b"t,ax,ay,az,wx,wy,wz\n"
        summary = summarize(validate_sidecar_zip(
            self._repack(inner), log_id="val-session_0", duration_ms=60_000))
        self.assertGreater(summary["counts"]["fail"], 0)


class GatesTests(unittest.TestCase):
    def _session(self, responder) -> minute_api.Session:
        sess = minute_api.Session({"idToken": "token"}, email="gate@example.com")
        sess._live = True
        sess.request = responder  # type: ignore[method-assign]
        return sess

    @staticmethod
    def _me_body(disabled: bool = False) -> str:
        # UserResponse do OpenAPI: resourceKey do próprio usuário + disabled topo
        # + organizations[].disabled por-org.
        return json.dumps({
            "email": "gate@example.com",
            "resourceKey": "gate-user-key",
            "disabled": False,
            "organizations": [
                {"resourceKey": "org", "name": "Org", "disabled": disabled},
            ],
        })

    @staticmethod
    def _quality_route() -> str:
        return "/api/v1/organizations/org/quality-screen"

    def test_org_state_detects_on_hold(self) -> None:
        def responder(method: str, path: str, body=None):
            if path == "/api/v1/users/me":
                return 200, self._me_body()
            if path == self._quality_route():
                return 200, json.dumps(
                    {"userState": "on_hold", "overall": "needs_work",
                     "scores": []})
            raise AssertionError(f"rota inesperada: {path}")

        state = self._session(responder).org_state("org")
        self.assertTrue(state["blocked"])
        self.assertEqual(state["userState"], "on_hold")

    def test_org_state_detects_inactive_and_disabled(self) -> None:
        def responder(method: str, path: str, body=None):
            if path == "/api/v1/users/me":
                return 200, self._me_body(disabled=True)
            if path == self._quality_route():
                return 200, json.dumps({"userState": "inactive"})
            raise AssertionError(f"rota inesperada: {path}")

        state = self._session(responder).org_state("org")
        self.assertTrue(state["disabled"])
        self.assertTrue(state["blocked"])

    def test_disabled_other_org_does_not_block_target(self) -> None:
        def responder(method: str, path: str, body=None):
            if path == "/api/v1/users/me":
                return 200, json.dumps({
                    "email": "gate@example.com",
                    "resourceKey": "gate-user-key",
                    "disabled": False,
                    "organizations": [
                        {"resourceKey": "outra", "name": "Outra",
                         "disabled": True},
                        {"resourceKey": "org", "name": "Org",
                         "disabled": False},
                    ],
                })
            if path == self._quality_route():
                return 200, json.dumps({"userState": "active"})
            raise AssertionError(f"rota inesperada: {path}")

        state = self._session(responder).org_state("org")
        self.assertFalse(state["disabled"])
        self.assertFalse(state["blocked"])

    def test_version_gate_latches_on_403_and_blocks(self) -> None:
        def responder(method: str, path: str, body=None):
            if path == "/api/v1/users/me":
                return 200, self._me_body()
            if path == self._quality_route():
                return 200, json.dumps({"userState": "active"})
            raise AssertionError(f"rota inesperada: {path}")

        sess = self._session(responder)
        try:
            minute_api._maybe_latch_version_gate(
                '{"detail":{"code":"app_version_too_old",'
                '"minVersion":"1.99.0"}}')
            gate = sess.version_gate()
            self.assertIsNotNone(gate)
            self.assertEqual(gate["minVersion"], "1.99.0")
            self.assertLess(
                minute_api._semver_tuple(config.APP_VERSION),
                minute_api._semver_tuple("1.99.0"))
            with self.assertRaises(minute_api.AuthError):
                sess.ensure_auth(org_key="org")
        finally:
            minute_api._maybe_latch_version_gate("", clear=True)

    def test_403_latches_through_session_request(self) -> None:
        def _jwt() -> str:
            def enc(obj: dict) -> str:
                import base64
                return base64.urlsafe_b64encode(
                    json.dumps(obj).encode()).rstrip(b"=").decode()
            return enc({"alg": "none"}) + "." + enc(
                {"exp": 4_000_000_000}) + ".sig"

        sess = minute_api.Session({"idToken": _jwt()}, email="gate@example.com")
        sess._live = True
        try:
            with mock.patch.object(
                    minute_api, "_request",
                    return_value=(
                        403,
                        '{"detail":{"code":"app_version_too_old",'
                        '"minVersion":"2.0.0"}}')):
                status, _ = sess.request("GET", "/api/v1/users/me")
            self.assertEqual(status, 403)
            gate = minute_api._version_gate_file()
            self.assertTrue(gate.exists())
            data = json.loads(gate.read_text(encoding="utf-8"))
            self.assertEqual(data["minVersion"], "2.0.0")
        finally:
            minute_api._maybe_latch_version_gate("", clear=True)

    def test_camera_policy_allow_and_deny(self) -> None:
        policy = json.dumps({
            "policyVersion": 1,
            "androidAllowModels": ["SM-S918B"],
            "androidDeniedModels": [],
            "androidAllowModelPatterns": [],
            "normalization": {
                "trimWhitespace": True, "lowercase": True,
                "collapseInternalWhitespace": True,
            },
        })

        def responder(method: str, path: str, body=None):
            if path == "/api/v1/devices/native-camera-policy":
                return 200, policy
            raise AssertionError(f"rota inesperada: {path}")

        sess = self._session(responder)
        self.assertIs(sess.camera_model_allowed("  sm-s918b  "), True)
        self.assertIs(sess.camera_model_allowed("SM-G991B"), False)


if __name__ == "__main__":
    unittest.main()
