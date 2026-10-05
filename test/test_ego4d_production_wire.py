"""Production Ego4D wire: shipped prepare→sidecar→upload_to_account path.

Drives real moneymin builders/validators. Auth/transport are stubbed open;
S3/Minute network is not required.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from moneymin import campaign, content_provenance, sidecar, validate
from moneymin.campaign_types import AccountSpec


UPTIME_NS = 224_584_000_000_000
STEP_NS = 2_000_000


def _relative_imu_csv(duration_ms: int = 100) -> str:
    n = max(2, int(duration_ms / 1000 * 500) + 1)
    rows = ["t,ax,ay,az,wx,wy,wz"]
    for i in range(n):
        rows.append(f"{i * STEP_NS},0.1,0.2,9.81,0.01,0.02,0.03")
    return "\n".join(rows) + "\n"


def _source_imu_csv(duration_ms: int = 100) -> str:
    lines = ["canonical_timestamp_ms,gyro_x,gyro_y,gyro_z,accl_x,accl_y,accl_z"]
    for t in range(0, duration_ms + 1, 10):
        lines.append(f"{t},0.01,0.02,0.03,0.1,0.2,9.81")
    return "\n".join(lines) + "\n"


class Ego4dPrepareNormalization(unittest.TestCase):
    def test_normalize_fills_prepare_required_fields_from_window(self):
        clip = {
            "clip_uid": "abc",
            "parent_video_uid": "parent",
            "window_s": (10.0, 70.0),
            "s3_path": "s3://ego4d-speac/public/v2/clips/abc.mp4",
        }
        out = campaign._normalize_ego_clip_for_prepare(clip)
        self.assertEqual(out["exported_clip_uid"], "abc")
        self.assertEqual(out["parent_start_sec"], "10.0")
        self.assertEqual(out["parent_end_sec"], "70.0")
        plan = campaign._ego_prepare_plan(out)
        self.assertEqual(plan["clip_uid"], "abc")
        self.assertAlmostEqual(plan["dur_s"], 60.0)


class Ego4dMirrorCandidates(unittest.TestCase):
    def test_clip_s3_candidates_include_speac_and_bristol(self):
        from moneymin import ego4d
        clip = {
            "clip_uid": "uid-1",
            "exported_clip_uid": "uid-1",
            "s3_path": "s3://ego4d-bristol/public/v1/clips/uid-1.mp4",
        }
        cands = ego4d._clip_s3_candidates(clip)
        buckets = {b for b, _ in cands}
        self.assertIn("ego4d-bristol", buckets)
        self.assertIn("ego4d-speac", buckets)
        self.assertTrue(any(k.endswith("uid-1.mp4") for _, k in cands))


class Ego4dPreferParentCuts(unittest.TestCase):
    def test_prefer_parent_cuts_orders_long_needs_cut_first(self):
        from moneymin import ego4d
        clips = [
            {"clip_uid": "short-export", "dur_s": 120.0, "needs_cut": False,
             "parent_video_uid": "p1", "media_uid": "export-media-1"},
            {"clip_uid": "long-parent", "dur_s": 900.0, "needs_cut": True,
             "parent_video_uid": "p2", "media_uid": "p2"},
        ]
        ordered = ego4d.prefer_long_clips(clips, prefer_parent_cuts=True)
        self.assertEqual(ordered[0]["clip_uid"], "long-parent")


class Ego4dImuCarve(unittest.TestCase):
    def test_carve_continuous_window_returns_subwindow(self):
        from moneymin import imu_coverage
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            # Continuous 0-2s and 5-10s; requested window 0-10 → carve longest 5-10.
            lines = ["canonical_timestamp_ms,gyro_x,gyro_y,gyro_z,accl_x,accl_y,accl_z"]
            for t in list(range(0, 2001, 10)) + list(range(5000, 10001, 10)):
                lines.append(f"{t},0.01,0.02,0.03,0.1,0.2,9.81")
            parent = "parent-carve"
            (root / f"{parent}_imu.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
            clip = {
                "clip_uid": "orig", "source": "ego4d",
                "parent_video_uid": parent, "window_s": (0.0, 10.0),
                "dur_s": 10.0, "needs_cut": True,
            }
            carved = imu_coverage.carve_continuous_window(
                clip, root, min_s=3.0, max_s=30.0)
            self.assertIsNotNone(carved)
            start, end = carved["window_s"]
            self.assertGreaterEqual(end - start, 3.0)
            self.assertGreaterEqual(start, 4.9)
            self.assertEqual(carved["imu_carved_from"], "orig")


class Ego4dCachedExpansionBestOf(unittest.TestCase):
    """v1.0.73/v2.0.2 accelerator merge restored into campaign selection."""

    def test_with_cached_expansion_appends_ready_uids(self):
        ranked = [{"clip_uid": "ranked-a", "dur_s": 120.0, "source": "ego4d"}]
        ready = [
            {"clip_uid": "ranked-a", "dur_s": 120.0, "source": "ego4d"},
            {"clip_uid": "accel-ready-b", "dur_s": 180.0, "source": "ego4d"},
        ]
        with patch.object(campaign, "ready_scenario_clips", create=True):
            pass
        with patch("moneymin.ego_accelerator.ready_scenario_clips",
                   return_value=ready) as ready_fn:
            merged = campaign._with_cached_expansion(
                ranked, "Gardening", min_dur_s=60, max_dur_s=1800,
                work_dir=Path("."), include_disabled=True)
        ready_fn.assert_called_once()
        uids = [c["clip_uid"] for c in merged]
        self.assertEqual(uids, ["ranked-a", "accel-ready-b"])


class Ego4dDeliveryGateNeutralized(unittest.TestCase):
    def test_require_dataset_native_delivery_support_is_noop(self):
        # Artificial receiver-policy gate must never abort production delivery.
        content_provenance.require_dataset_native_delivery_support(None)
        content_provenance.require_dataset_native_delivery_support({"broken": True})
        content_provenance.require_dataset_native_delivery_support("not-a-dict")

    def test_sidecar_builder_ignores_derived_diagnostics_payload(self):
        blob = sidecar.build_sidecar_zip_custom(
            session_id="prod-sid",
            chunk_index=0,
            duration_ms=100,
            recorded_at="2026-10-04T00:00:00.000Z",
            imu_csv=_relative_imu_csv(100),
            frames_csv=f"i,ptsNs,dtNs,tNs,key\n0,0,0,{UPTIME_NS},1\n",
            derived_diagnostics={"kind": "garbage", "uniform_grid_step_ns": 1},
            uptime_ns=UPTIME_NS,
            platform_meta={"os": "android", "version": 34},
        )
        self.assertIsInstance(blob, bytes)
        self.assertGreater(len(blob), 64)


class Ego4dSidecarMinute129Wire(unittest.TestCase):
    def test_relative_imu_lands_on_elapsed_realtime_domain(self):
        duration_ms = 100
        imu_rel = _relative_imu_csv(duration_ms)
        frames = (
            "i,ptsNs,dtNs,tNs,key\n"
            f"0,0,0,{UPTIME_NS},1\n"
            f"1,{duration_ms * 1_000_000},{duration_ms * 1_000_000},"
            f"{UPTIME_NS + duration_ms * 1_000_000},0\n"
        )
        blob = sidecar.build_sidecar_zip_custom(
            session_id="wire-session",
            chunk_index=0,
            duration_ms=duration_ms,
            recorded_at="2026-10-04T12:00:00.000Z",
            imu_csv=imu_rel,
            frames_csv=frames,
            uptime_ns=UPTIME_NS,
            video_probe={"width": 1440, "height": 1080, "fps": 30.0, "bitrate": 8_000_000},
            platform_meta={"os": "android", "version": 34},
            device_meta={"model": "SM-S901E", "systemName": "Android", "systemVersion": "14"},
        )
        log_id = "wire-session_0"
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = set(zf.namelist())
            self.assertEqual(
                names,
                {f"{log_id}.metadata.json", f"{log_id}.imu.csv", f"{log_id}.frames.csv"},
            )
            meta = json.loads(zf.read(f"{log_id}.metadata.json"))
            imu_text = zf.read(f"{log_id}.imu.csv").decode("utf-8")

        first_imu = int(imu_text.splitlines()[1].split(",")[0])
        anchor = int(meta["timebase"]["firstFrameSensorTimestampNs"])
        self.assertEqual(first_imu, UPTIME_NS)
        self.assertEqual(anchor, UPTIME_NS)
        self.assertEqual(meta["timebase"]["clockDomain"], "android_elapsedRealtimeNanos")
        self.assertEqual(meta["imuDiagnostics"]["maxInterpolationSpanNs"], "25000000")
        self.assertNotIn("clockOffsetNs", meta["imuDiagnostics"])
        self.assertEqual(meta["platform"], {"os": "android", "version": 34})

        checks = {
            c.name: c
            for c in validate.validate_sidecar_zip(blob, log_id=log_id, duration_ms=duration_ms)
        }
        self.assertEqual(checks["xcheck.imu_timebase"].status, "pass")
        self.assertNotIn("imuDiagnostics.no_clockOffsetNs", checks)
        fails = [c for c in checks.values() if c.status == "fail"]
        self.assertEqual(fails, [], msg="; ".join(f"{c.name}:{c.detail}" for c in fails))


class Ego4dForgeEnvelope(unittest.TestCase):
    """Minute-rigid forged fields: device/timebase/IMU diagnostics from resample."""

    def test_resample_stats_feed_imu_diagnostics_in_zip(self):
        with tempfile.TemporaryDirectory(prefix="ego4d-forge-") as folder:
            root = Path(folder)
            # Dense gyro; accel has a 20 ms hole → interpolated, not zero-lie.
            src = root / "imu.csv"
            lines = ["canonical_timestamp_ms,gyro_x,gyro_y,gyro_z,accl_x,accl_y,accl_z"]
            for t in range(0, 101, 2):
                accel = ",," if 40 < t < 60 else "0.1,0.2,9.81"
                lines.append(f"{t},0.01,0.02,0.03,{accel}")
            src.write_text("\n".join(lines) + "\n", encoding="utf-8")
            from moneymin import ego4d
            stats: dict = {}
            imu_rel = ego4d.build_imu_csv(src, (0.0, 0.1), duration_ms=100, stats=stats)
            self.assertGreater(stats["interpolatedCount"], 0)
            self.assertEqual(stats["strategy"], "gyro_anchored_v1")
            self.assertEqual(stats["sampleCount"], 51)

            blob = sidecar.build_sidecar_zip_custom(
                session_id="forge-session",
                chunk_index=0,
                duration_ms=100,
                recorded_at="2026-10-04T12:00:00.000Z",
                imu_csv=imu_rel,
                frames_csv=(
                    "i,ptsNs,dtNs,tNs,key\n"
                    f"0,0,0,{UPTIME_NS},1\n"
                    f"1,100000000,100000000,{UPTIME_NS + 100000000},0\n"
                ),
                uptime_ns=UPTIME_NS,
                imu_diagnostics=stats,
                imu_sample_count=stats["sampleCount"],
                video_probe={"width": 1440, "height": 1080, "fps": 30.0, "bitrate": 8_000_000},
                platform_meta={"os": "android", "version": 34},
                device_meta={
                    "model": "SM-S901E", "systemName": "Android", "systemVersion": "14",
                },
            )
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                meta = json.loads(zf.read("forge-session_0.metadata.json"))
                imu_out = zf.read("forge-session_0.imu.csv").decode()
            diag = meta["imuDiagnostics"]
            self.assertEqual(diag["strategy"], "gyro_anchored_v1")
            self.assertEqual(diag["sampleCount"], stats["sampleCount"])
            self.assertEqual(diag["interpolatedCount"], stats["interpolatedCount"])
            self.assertGreater(diag["interpolatedCount"], 0)
            # APK EgoImu always emits config ceiling, never measured span.
            self.assertEqual(diag["maxInterpolationSpanNs"], "25000000")
            self.assertNotEqual(
                diag["maxInterpolationSpanNs"],
                stats.get("measuredMaxInterpolationSpanNs"))
            self.assertNotIn("clockOffsetNs", diag)
            self.assertEqual(meta["device"]["model"], "SM-S901E")
            self.assertEqual(meta["platform"], {"os": "android", "version": 34})
            self.assertEqual(meta["appVersion"], "1.29.0")
            self.assertEqual(meta["timebase"]["clockDomain"], "android_elapsedRealtimeNanos")
            self.assertEqual(int(imu_out.splitlines()[1].split(",")[0]), UPTIME_NS)
            self.assertEqual(
                int(meta["timebase"]["firstFrameSensorTimestampNs"]), UPTIME_NS)
            # Writer order: imu → frames → metadata
            with zipfile.ZipFile(io.BytesIO(blob)) as zf2:
                names = [i.filename for i in zf2.infolist()]
            self.assertEqual(
                names,
                ["forge-session_0.imu.csv",
                 "forge-session_0.frames.csv",
                 "forge-session_0.metadata.json"])

    def test_forge_map_document_exists(self):
        root = Path(__file__).resolve().parents[1]
        doc = root / "EGO4D_FORGE_MAP.md"
        self.assertTrue(doc.is_file())
        text = doc.read_text(encoding="utf-8")
        for needle in (
            "imuDiagnostics", "android_elapsedRealtimeNanos", "SM-S901E",
            "500 Hz", "Brown-Conrady", "getDeviceUploadMeta",
        ):
            self.assertIn(needle, text)


class Ego4dUploadReachesSession(unittest.TestCase):
    def test_prepared_ego4d_item_invokes_upload_session(self):
        with tempfile.TemporaryDirectory(prefix="ego4d-prod-wire-") as folder:
            root = Path(folder)
            media = root / "prepared.mp4"
            media.write_bytes(b"declared-prepared-mp4-bytes-not-decoded")
            sensor = root / "parent_imu.csv"
            sensor.write_text(_source_imu_csv(100), encoding="utf-8")
            imu_out = _relative_imu_csv(100)
            frames_placeholder = "i,ptsNs,dtNs,tNs,key\n0,0,0,0,1\n"
            item = content_provenance.prepare_content_provenance(
                media, sensor, media, imu_out, frames_placeholder,
                clip_uid="clip-prod", parent_video_uid="parent-prod",
                media_uid="media-prod", window_s=(0.0, 0.1),
                media_offset_s=0.0, normalization_start_s=0.0,
                selection_evidence=None,
            )
            item.update(
                source="ego4d",
                imu_real=True,
                clip_uid="clip-prod",
                video_path=str(media),
                duration_ms=60_000,
                n_samples=item["derived_diagnostics"]["output"]["sample_count"],
                probe={"duration_ms": 60_000, "fps": 30.0, "width": 1440, "height": 1080},
                _content_candidate={"clip_uid": "clip-prod", "parent_video_uid": "parent-prod"},
                task_name_authoritative="Gardening",
                registry_key="prod-registry",
            )

            profile = SimpleNamespace(
                frames_gop=30,
                uptime_ns_at=lambda _wall: UPTIME_NS,
                calib={},
                sidecar_device_meta=lambda: {
                    "model": "SM-S901E", "systemName": "Android", "systemVersion": "14",
                },
                sidecar_platform_meta=lambda: {"os": "android", "version": 34},
            )
            session = SimpleNamespace(
                _live=True,
                _moneymin_pending_pumped=True,
                recording_policy=None,
                email="prod@example.invalid",
                warmup=lambda: None,
                ensure_auth=lambda **_k: None,
            )
            account = AccountSpec("prod@example.invalid", "prod-org")

            with patch.object(campaign.ego4d, "revalidate_selection_evidence",
                              return_value={"schema": 1, "task": {"name": "Gardening"},
                                            "physical_provenance_verified": False}), \
                 patch.object(campaign.org_policy, "account_kind", return_value="other"), \
                 patch.object(campaign.device_profile, "get_profile", return_value=profile), \
                 patch.object(campaign, "_new_identity",
                              return_value=("prod-session", "prod-session_0",
                                            "2026-10-04T12:00:00.000Z")), \
                 patch.object(campaign, "_chunk_plan", return_value=[(0, 60_000)]), \
                 patch.object(campaign, "probe_video", return_value={
                     "duration_ms": 60_000, "fps": 30.0, "has_video": True,
                     "width": 1440, "height": 1080}), \
                 patch.object(campaign, "build_frames_csv_from_video",
                              return_value=(
                                  "i,ptsNs,dtNs,tNs,key\n"
                                  f"0,0,0,{UPTIME_NS},1\n"
                                  f"1,60000000000,60000000000,{UPTIME_NS + 60_000_000_000},0\n"
                              )), \
                 patch.object(campaign, "upload_session") as upload_session:
                upload_session.return_value = SimpleNamespace(
                    session_id="prod-session", finalized=True, finalize_status=204,
                    chunks=[SimpleNamespace(
                        state="done", error=None, upload_id="up-1",
                        evaluate_result={"checks": []})],
                )
                # Prove the neutralized gate cannot abort the path.
                content_provenance.require_dataset_native_delivery_support(
                    item["derived_diagnostics"])
                result = campaign.upload_to_account(
                    item, account, "task-prod", 30, True, True,
                    session=session, recover_pending=False)

            self.assertTrue(result["ok"], msg=result.get("error"))
            upload_session.assert_called_once()
            kwargs = upload_session.call_args.kwargs
            self.assertTrue(kwargs.get("sidecar"))
            self.assertTrue(kwargs.get("suppress_per_chunk_catbear"))
            self.assertFalse(kwargs.get("normalize"))


if __name__ == "__main__":
    unittest.main()
