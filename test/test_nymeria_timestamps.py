"""Offline proofs for measured Nymeria RGB/IMU clocks and VFR encoding."""
from __future__ import annotations

import csv
import io
import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import numpy as np

from moneymin import campaign, content_provenance, nymeria_vrs, sidecar


def imu_samples(start_ns: int, end_ns: int, *, step_ns: int = 1_000_000,
                value_origin_ns: int = 0):
    return [(ts, ((ts - value_origin_ns) / 1e6, 2., 3.), (4., 5., 6.))
            for ts in range(start_ns, end_ns + 1, step_ns)]


class FakeProvider:
    def __init__(self, *, images=None, imu=None):
        self.images = images
        self.imu = imu
        self.read_indices = []

    def get_stream_id_from_label(self, label):
        return label

    def get_num_data(self, sid):
        return len(self.images if self.images is not None else self.imu)

    def get_timestamps_ns(self, sid, domain):
        return [row[0] for row in (self.images if self.images is not None else self.imu)]

    def get_image_data_by_index(self, sid, idx):
        self.read_indices.append(idx)
        ts, rgb = self.images[idx]
        return types.SimpleNamespace(to_numpy_array=lambda: rgb), types.SimpleNamespace(
            capture_timestamp_ns=ts)

    def get_imu_data_by_index(self, sid, idx):
        self.read_indices.append(idx)
        ts, accel, gyro, valid = self.imu[idx]
        return types.SimpleNamespace(capture_timestamp_ns=ts, accel_msec2=accel,
                                     gyro_radsec=gyro, accel_valid=valid, gyro_valid=valid)


def sdk_patch():
    return patch.dict(sys.modules, {
        "projectaria_tools.core.sensor_data": types.SimpleNamespace(
            TimeDomain=types.SimpleNamespace(DEVICE_TIME="DEVICE_TIME"))})


class NymeriaResampleTimestampsTests(unittest.TestCase):
    def test_first_rgb_origin_controls_values_and_real_alignment(self):
        samples = imu_samples(9_999_500_000, 10_125_500_000,
                              value_origin_ns=10_000_000_000)
        stats = {}
        csv_text = nymeria_vrs.build_imu_csv_from_samples(
            samples, t0_ns=10_005_000_000, duration_ms=120, stats=stats)
        rows = list(csv.DictReader(io.StringIO(csv_text)))
        self.assertEqual(len(rows), 61)
        self.assertEqual([int(row["t"]) for row in rows], list(range(0, 120_000_001, 2_000_000)))
        self.assertEqual(float(rows[0]["ax"]), 5.)
        self.assertEqual(float(rows[-1]["ax"]), 125.)
        self.assertEqual(stats["sourceOriginNs"], "10005000000")
        self.assertEqual(stats["maxAlignmentDeltaNs"], "500000")
        self.assertEqual(stats["p95AlignmentDeltaNs"], "500000")
        self.assertEqual(stats["measuredMaxInterpolationSpanNs"], "1000000")
        self.assertEqual(stats["interpolatedCount"], 61)
        self.assertEqual(stats["nearestFallbackCount"], 0)

    def test_gap_ceiling_and_uncovered_edges_fail_closed(self):
        for samples, origin, duration in [
                (imu_samples(0, 10_000_000) + imu_samples(36_000_000, 60_000_000), 0, 60),
                (imu_samples(2_000_000, 60_000_000), 0, 60),
                (imu_samples(0, 58_000_000), 0, 60),
                (imu_samples(0, 60_000_000)[::-1], 0, 60),
                ([(0, (float("nan"), 0., 0.), (0., 0., 0.)),
                  (2_000_000, (0., 0., 0.), (0., 0., 0.))], 0, 2)]:
            with self.subTest(origin=origin, sample_count=len(samples)):
                with self.assertRaises(RuntimeError):
                    nymeria_vrs.build_imu_csv_from_samples(
                        samples, t0_ns=origin, duration_ms=duration)
        samples = [(0, (0., 0., 0.), (0., 0., 0.)),
                   (25_000_000, (25., 0., 0.), (0., 0., 0.)),
                   (26_000_000, (26., 0., 0.), (0., 0., 0.))]
        stats = {}
        nymeria_vrs.build_imu_csv_from_samples(samples, duration_ms=26, stats=stats)
        self.assertEqual(stats["measuredMaxInterpolationSpanNs"], "25000000")
        self.assertGreater(int(stats["maxAlignmentDeltaNs"]), 1_000_000)

    def test_chunk_diagnostics_follow_half_open_targets_and_actual_drops(self):
        samples = imu_samples(0, 8_000_000) + imu_samples(12_000_000, 24_000_000)
        stats = {}
        full = nymeria_vrs.build_imu_csv_from_samples(
            samples, duration_ms=24, stats=stats,
            source_dropped_timestamps_ns=[9_000_000, 12_000_000, 24_000_000],
            stats_windows_ms=[(0, 12), (12, 24)])
        first, second = stats["windows"]
        self.assertEqual(stats["sampleCount"], 13)
        self.assertEqual(first["sampleCount"], 6)
        self.assertEqual(second["sampleCount"], 6)
        self.assertEqual(first["droppedRowCount"], 1)
        self.assertEqual(second["droppedRowCount"], 1)
        self.assertEqual(stats["droppedRowCount"], 3)
        self.assertEqual(first["interpolatedCount"], 1)
        self.assertEqual(second["interpolatedCount"], 0)
        self.assertEqual(first["measuredMaxInterpolationSpanNs"], "4000000")
        self.assertEqual(second["measuredMaxInterpolationSpanNs"], "0")
        self.assertEqual(campaign._slice_imu_csv(full, 0, 12)[1], first["sampleCount"])
        self.assertEqual(campaign._slice_imu_csv(full, 12, 12)[1], second["sampleCount"])

    def test_reader_retains_real_brackets_and_uses_index(self):
        source = [(ts, accel, gyro, ts != 50_000_000)
                  for ts, accel, gyro in imu_samples(0, 100_000_000)]
        provider = FakeProvider(imu=source)
        stats = {}
        with sdk_patch(), patch.object(nymeria_vrs, "_provider", return_value=provider):
            selected = nymeria_vrs.read_imu_samples(
                Path("motion.vrs"), t0_ns=49_500_000, t1_ns=52_500_000, stats=stats)
        self.assertEqual([row[0] for row in selected], [49_000_000, 51_000_000, 52_000_000, 53_000_000])
        self.assertEqual(stats["droppedRowTimestampsNs"], [50_000_000])
        self.assertLess(len(provider.read_indices), len(source))


@unittest.skipUnless(Path(sidecar.ffmpeg_bin()).is_file() or shutil.which(sidecar.ffmpeg_bin()),
                     "FFmpeg indisponível")
class NymeriaMeasuredVideoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nymeria-timestamps-")
        self.root = Path(self.temp.name)
        self.vrs = self.root / "data.vrs"
        self.vrs.write_bytes(b"inert VRS identity fixture")
        self.origin = 10_000_000_000
        self.images = [(self.origin + delta * 1_000_000,
                        np.full((16, 16, 4), index * 40, dtype=np.uint8))
                       for index, delta in enumerate([5, 35, 75, 100])]

    def tearDown(self):
        self.temp.cleanup()

    def test_vfr_mp4_preserves_captures_and_last_packet_duration(self):
        provider = FakeProvider(images=self.images)
        out = self.root / "out.mp4"
        stats = {}
        with sdk_patch(), patch.object(nymeria_vrs, "_provider", return_value=provider):
            nymeria_vrs.extract_rgb_mp4(
                self.vrs, out, t0_ns=self.origin, t1_ns=self.origin + 125_000_000,
                stats=stats)
        frames = sidecar._extract_frame_pts(out)
        self.assertEqual([frame[0] for frame in frames], [0, 30_000_000, 70_000_000, 95_000_000])
        self.assertEqual(sidecar.probe_video(out)["duration_ms"], 120)
        self.assertEqual(stats["firstCaptureTimestampNs"], str(self.origin + 5_000_000))
        self.assertEqual(stats["frameCount"], 4)
        self.assertEqual(stats["measuredMaxFrameGapNs"], "40000000")
        self.assertFalse(list(self.root.glob(".nymeria-*")))

    def test_invalid_rgb_and_encode_failure_preserve_existing_output(self):
        out = self.root / "out.mp4"
        out.write_bytes(b"previous prepared media")
        for images in [self.images[:1], [self.images[0], self.images[0]],
                       [(self.origin + delta * 1_000_000, rgb)
                        for delta, (_, rgb) in zip([5, 135, 175, 200], self.images)],
                       [self.images[0], (self.images[1][0], np.zeros((16, 16), dtype=np.uint8))]]:
            with self.subTest(frames=len(images)):
                with sdk_patch(), patch.object(nymeria_vrs, "_provider", return_value=FakeProvider(images=images)):
                    with self.assertRaises(RuntimeError):
                        nymeria_vrs.extract_rgb_mp4(
                            self.vrs, out, t0_ns=self.origin, t1_ns=self.origin + 225_000_000,
                            max_frame_gap_ms=100)
                self.assertEqual(out.read_bytes(), b"previous prepared media")
                self.assertFalse(list(self.root.glob(".nymeria-*")))
        with sdk_patch(), patch.object(nymeria_vrs, "_provider", return_value=FakeProvider(images=self.images)):
            with self.assertRaises(RuntimeError):
                nymeria_vrs.extract_rgb_mp4(
                    self.vrs, out, t0_ns=self.origin, t1_ns=self.origin + 125_000_000,
                    output_args=["-c:v", "inert_missing_encoder"])
        self.assertEqual(out.read_bytes(), b"previous prepared media")

    def test_measured_frame_gap_is_preserved_without_invented_rgb_limit(self):
        images = [(self.origin + delta * 1_000_000, rgb)
                  for delta, (_, rgb) in zip([5, 135, 175, 200], self.images)]
        out = self.root / "measured-gap.mp4"
        stats = {}
        with sdk_patch(), patch.object(nymeria_vrs, "_provider", return_value=FakeProvider(images=images)):
            nymeria_vrs.extract_rgb_mp4(
                self.vrs, out, t0_ns=self.origin, t1_ns=self.origin + 225_000_000,
                stats=stats)
        self.assertEqual([pts for pts, _ in sidecar._extract_frame_pts(out)],
                         [0, 130_000_000, 170_000_000, 195_000_000])
        self.assertEqual(stats["frameCount"], 4)
        self.assertEqual(stats["measuredMaxFrameGapNs"], "130000000")
        self.assertEqual(stats["measuredLeadingFrameGapNs"], "5000000")
        self.assertEqual(stats["measuredTrailingFrameGapNs"], "25000000")

    def test_aria_presentation_rotates_clockwise_without_changing_pts(self):
        rgb = np.zeros((16, 24, 3), dtype=np.uint8)
        rgb[:8, :12] = (255, 0, 0)
        rgb[:8, 12:] = (0, 255, 0)
        rgb[8:, :12] = (0, 0, 255)
        rgb[8:, 12:] = (255, 255, 0)
        images = [(ts, rgb) for ts, _ in self.images]
        out = self.root / "oriented.mp4"
        stats = {}
        with sdk_patch(), patch.object(nymeria_vrs, "_provider", return_value=FakeProvider(images=images)):
            nymeria_vrs.extract_rgb_mp4(
                self.vrs, out, t0_ns=self.origin, t1_ns=self.origin + 125_000_000,
                stats=stats)
        probe = sidecar.probe_video(out)
        self.assertEqual((probe["width"], probe["height"]), (16, 24))
        decoded = subprocess.run([
            sidecar.ffmpeg_bin(), "-v", "error", "-i", str(out), "-frames:v", "1",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
            capture_output=True, check=True).stdout
        frame = np.frombuffer(decoded, dtype=np.uint8).reshape(24, 16, 3)
        for point, color in [((6, 4), (0, 0, 255)), ((6, 12), (255, 0, 0)),
                             ((18, 4), (255, 255, 0)), ((18, 12), (0, 255, 0))]:
            self.assertLess(np.max(np.abs(frame[point].astype(int) - color)), 12)
        self.assertEqual([pts for pts, _ in sidecar._extract_frame_pts(out)],
                         [0, 30_000_000, 70_000_000, 95_000_000])
        self.assertEqual(stats["presentationRotationDegreesClockwise"], 90)

    def test_prepare_uses_first_rgb_measured_frames_and_bound_sources(self):
        seq = self.root / "seq"
        data_dir = seq / "recording_head" / "data"
        data_dir.mkdir(parents=True)
        (data_dir / "data.vrs").write_bytes(self.vrs.read_bytes())
        (data_dir / "motion.vrs").write_bytes(b"inert measured IMU identity fixture")
        imu = [(ts, accel, gyro, True) for ts, accel, gyro in imu_samples(
            self.origin - 500_000, self.origin + 125_500_000, value_origin_ns=self.origin)]
        clip = {"clip_uid": "nymeria:seq:0.000:0.125", "seq_id": "seq", "path": str(seq),
                "window_s": [0., .125], "source": "nymeria",
                "task_name_authoritative": "fixture action", "task_id": "inert-task",
                "registry_key": "inert-registry",
                "selection_evidence": {"task": {"name": "fixture action"}}}
        def provider(path):
            return FakeProvider(images=self.images) if path.name == "data.vrs" else FakeProvider(imu=imu)
        with sdk_patch(), patch.object(nymeria_vrs, "_provider", side_effect=provider), \
                patch.object(campaign.nymeria, "device_window_for_sequence",
                             return_value=(self.origin, self.origin + 125_000_000)), \
                patch.object(campaign.nymeria, "revalidate_candidate", return_value=clip, create=True), \
                patch.object(campaign, "MIN_DUR_MS", 100), \
                patch.object(campaign, "_normalize_video", side_effect=AssertionError("CFR normalizer forbidden")), \
                patch.object(campaign, "_frames_csv", side_effect=AssertionError("estimated frames forbidden")):
            item = campaign.prepare_nymeria_clip(clip, self.root / "prepared", allow_download=False)
        self.assertEqual(item["duration_ms"], 120)
        self.assertEqual(item["task_name_authoritative"], "fixture action")
        self.assertEqual(item["task_id"], "inert-task")
        self.assertEqual(item["registry_key"], "inert-registry")
        self.assertEqual(float(list(csv.DictReader(io.StringIO(item["imu_csv"])))[0]["ax"]), 5.)
        self.assertEqual([int(row["ptsNs"]) for row in csv.DictReader(io.StringIO(item["frames_csv"]))],
                         [0, 30_000_000, 70_000_000, 95_000_000])
        self.assertEqual(item["_nymeria_resample_inputs"]["origin_ns"], self.origin + 5_000_000)
        self.assertEqual(item["n_samples"], 61)
        content_provenance.revalidate_content_provenance(item)
        # The wire builder must add one account uptime to both measured
        # clocks; the prepared CSV itself stays relative and unchanged.
        uptime = 123_000_000_000
        original_imu = item["imu_csv"]
        frames_at_uptime = sidecar.build_frames_csv_from_video(
            item["video_path"], duration_ms=120, offset_ns=uptime,
            require_measured_pts=True)
        payload = sidecar.build_sidecar_zip_custom(
            session_id="inert-nymeria", chunk_index=0, log_id="inert-nymeria_0",
            duration_ms=120, recorded_at="2026-10-06T12:00:00.000Z",
            video_probe=item["probe"], imu_csv=item["imu_csv"],
            frames_csv=frames_at_uptime, imu_diagnostics=item["imu_diagnostics"],
            uptime_ns=uptime)
        from moneymin.validate import validate_sidecar_zip
        checks = validate_sidecar_zip(payload, log_id="inert-nymeria_0", duration_ms=120)
        self.assertFalse([check for check in checks if check.status == "fail"])
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            wire_imu = list(csv.DictReader(io.StringIO(archive.read(
                "inert-nymeria_0.imu.csv").decode())))
            wire_frames = list(csv.DictReader(io.StringIO(archive.read(
                "inert-nymeria_0.frames.csv").decode())))
            metadata = json.loads(archive.read("inert-nymeria_0.metadata.json"))
        self.assertEqual(int(wire_imu[0]["t"]), uptime)
        self.assertEqual(int(wire_frames[0]["tNs"]), uptime)
        self.assertEqual(int(wire_frames[-1]["ptsNs"]), 95_000_000)
        self.assertEqual(metadata["imuDiagnostics"]["maxAlignmentDeltaNs"], "500000")
        self.assertEqual(metadata["imuDiagnostics"]["sampleCount"], 61)
        self.assertEqual(item["imu_csv"], original_imu)
        (data_dir / "motion.vrs").write_bytes(b"changed source")
        with self.assertRaises(ValueError):
            content_provenance.revalidate_content_provenance(item)

    def test_missing_fresh_action_proof_stops_before_vrs_decode(self):
        seq = self.root / "unproved-seq"
        data_dir = seq / "recording_head" / "data"
        data_dir.mkdir(parents=True)
        for name in ("data.vrs", "motion.vrs"):
            (data_dir / name).write_bytes(b"inert source identity")
        clip = {"clip_uid": "nymeria:unproved-seq:0.000:0.125", "seq_id": "unproved-seq",
                "path": str(seq), "window_s": [0., .125], "source": "nymeria"}
        with patch.object(campaign, "MIN_DUR_MS", 100), \
                patch.object(campaign.nymeria, "revalidate_candidate",
                             side_effect=ValueError("missing fresh action proof"), create=True), \
                patch.object(nymeria_vrs, "_provider") as provider:
            with self.assertRaisesRegex(ValueError, "missing fresh action proof"):
                campaign.prepare_nymeria_clip(clip, self.root / "prepared", allow_download=False)
        provider.assert_not_called()
        self.assertFalse(list((self.root / "prepared").glob("*.mp4")))


if __name__ == "__main__":
    unittest.main()
