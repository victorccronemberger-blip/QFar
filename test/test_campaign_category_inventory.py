"""Category inventory does not read bulk media; byte fixtures are inert."""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from moneymin import campaign, ego4d, ego_accelerator, nymeria, task_matching


class CategoryInventoryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.work = self.root / "ego4d"
        self.work.mkdir()
        self.stack.enter_context(patch.object(campaign.config, "DATA_DIR", self.root))
        self.stack.enter_context(patch.object(campaign.config, "MEDIA_DATA_DIR", self.root))
        self.stack.enter_context(patch.object(ego4d, "EGO4D_DIR", self.work))
        self.clip = {"clip_uid": "inert-clip", "exported_clip_uid": "inert-clip",
                     "parent_video_uid": "inert-parent", "dur_s": 300.0,
                     "source": "ego4d", "parent_start_sec": "0",
                     "parent_end_sec": "300", "window_s": [0.0, 300.0],
                     "needs_cut": False, "s3_path": "s3://fixture/inert.mp4"}
        self.source = self.work / "inert-clip.mp4"
        self.native = self.work / "inert-clip_native.mp4"
        self.source.write_bytes(b"S" * (1024 * 1024 + 9))
        self.native.write_bytes(b"N" * (1024 * 1024 + 11))
        self.imu = self.work / "inert-parent_imu.csv"
        self.imu.write_text("canonical_timestamp_ms,gyro_x,gyro_y,gyro_z,accl_x,accl_y,accl_z\n"
                            + "0,0,0,0,0,0,0\n" * 20, encoding="utf-8")
        saved = {"version": 5, "source_size": self.source.stat().st_size,
                 "source_mtime_ns": self.source.stat().st_mtime_ns,
                 "source_sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
                 "start_s": None, "dur_s": 300.0, "width": 1440,
                 "height": 1080, "fps": 30,
                 "prepared_size": self.native.stat().st_size,
                 "prepared_sha256": hashlib.sha256(self.native.read_bytes()).hexdigest()}
        self.marker = self.native.with_name(self.native.name + ".source.json")
        self.marker.write_text(json.dumps(saved), encoding="utf-8")
        names = json.loads((Path(__file__).parent / "current_minute_task_names.json").read_text())
        self.tasks = [{"id": str(index), "name": name} for index, name in enumerate(names)]

    def no_bulk_reads(self):
        opened = Path.open

        def inventory_open(path, *args, **kwargs):
            if path.suffix.lower() in (".mp4", ".vrs") or path == self.imu:
                raise AssertionError("category listing opened bulk media: " + path.name)
            return opened(path, *args, **kwargs)

        self.stack.enter_context(patch.object(Path, "open", inventory_open))
        for target in ("_native_cache_fingerprint", "probe_video", "build_frames_csv_from_video"):
            self.stack.enter_context(patch.object(campaign, target,
                side_effect=AssertionError("category listing measured media")))
        self.stack.enter_context(patch.object(ego4d, "_valid_imu_cache",
            side_effect=AssertionError("category listing parsed IMU")))
        self.stack.enter_context(patch.object(ego4d, "sync_meta",
            side_effect=AssertionError("category listing acquired sources")))

    def test_all_categories_duration_changes_use_real_expansion_without_bulk_reads(self):
        self.no_bulk_reads()
        self.stack.enter_context(patch.object(campaign, "_compatible_task_clips",
                                             return_value=[self.clip]))
        buckets = {task_matching.canonical_task_name(row["name"]): [self.clip]
                   for row in self.tasks}
        self.stack.enter_context(patch.object(ego_accelerator, "scenario_buckets",
                                             return_value=buckets))
        # The test exercises inventory, not task evidence. No fixture is sent.
        self.stack.enter_context(patch.object(ego4d, "revalidate_selection_evidence",
            side_effect=lambda clip, **kwargs: clip["selection_evidence"]))
        with patch.object(campaign, "ego_clip_cache_state",
                          side_effect=AssertionError("category listing used full cache validation")), \
             patch.object(ego_accelerator, "ready_scenario_clips",
                          wraps=ego_accelerator.ready_scenario_clips) as expansion:
            for bounds in ((300, 1800), (300, 600), (300, 1800)):
                for mode in ("both", "cache", "dataset"):
                    rows = campaign.available_tasks("fixture@example.invalid", "fixture-org",
                        remote_tasks=self.tasks, dataset_provider="ego4d", content_mode=mode,
                        min_dur_s=bounds[0], max_dur_s=bounds[1], include_unavailable=True)
                    self.assertEqual(len(rows), len(self.tasks))
                    supported = [row for row in rows if row["mapping_supported"]]
                    self.assertTrue(supported)
                    self.assertTrue(all(row["clip_count"] == 1 for row in supported))
                    self.assertTrue(all(row["requires_measured_validation"] for row in supported))
            self.assertTrue(expansion.call_count)
            self.assertTrue(all(call.kwargs["catalog_only"] for call in expansion.call_args_list))

    def test_hint_is_not_same_stat_source_admission(self):
        self.assertTrue(campaign._catalog_clip_cached_hint(self.clip, self.work))
        before = self.source.stat()
        self.source.write_bytes(b"X" * before.st_size)
        os.utime(self.source, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertTrue(campaign._catalog_clip_cached_hint(self.clip, self.work))
        with patch.object(campaign, "_ego_clip_inputs", return_value=(self.clip, {})):
            self.assertEqual(campaign.ego_clip_cache_state(self.clip, self.work), "partial")

    def test_inventory_rejects_missing_sources_and_legacy_or_malformed_markers(self):
        saved = self.marker.read_text(encoding="utf-8")
        for value in ({"version": 4}, {"version": 5}, [], None):
            self.marker.write_text(json.dumps(value), encoding="utf-8")
            self.assertFalse(campaign._catalog_clip_cached_hint(self.clip, self.work))
        self.marker.write_text(saved, encoding="utf-8")
        self.imu.unlink()
        self.assertFalse(campaign._catalog_clip_cached_hint(self.clip, self.work))

    def test_planned_catalog_does_not_rebuild_per_candidate_and_runtime_still_does(self):
        name = "Gardening"
        device_window = [20_000_000_000, 320_000_000_000]
        uid = "nymeria-planned:inert-sequence:20000000000:320000000000"
        clip = {"clip_uid": uid, "source": "nymeria", "seq_id": "inert-sequence",
                "task_name_authoritative": name, "dur_s": 300.0,
                "window_s": [0.0, 300.0], "device_window_ns": device_window,
                "planned_device_window_ns": device_window,
                "source_clock_domain": "aria_DEVICE_TIME_ns",
                "acquisition_required": True, "selection_ready": False,
                "selection_evidence": {"schema": 1, "dataset": "nymeria",
                    "algorithm": nymeria._PLANNED_ALGORITHM, "task": {"name": name},
                    "sensor_coverage": "unmeasured", "duration_bounds_s": [300.0, 1800.0],
                    "planned_device_window_ns": device_window}}
        with patch.object(campaign, "_compatible_task_clips", return_value=[clip]), \
             patch.object(nymeria, "revalidate_planned_candidate",
                          side_effect=AssertionError("catalog rebuilt the provider plan")):
            rows = campaign.available_tasks("fixture@example.invalid", "fixture-org",
                remote_tasks=[{"id": "task", "name": name}], dataset_provider="nymeria",
                content_mode="dataset", min_dur_s=300, max_dur_s=1800)
            self.assertEqual(rows[0]["clip_count"], 1)
        with patch.object(nymeria, "revalidate_planned_candidate",
                          side_effect=ValueError("runtime proof changed")) as revalidate:
            self.assertFalse(campaign._prepare_queue_accepts(clip, name))
            revalidate.assert_called_once()
        self.assertFalse(campaign._catalog_queue_accepts(clip, "Walk the Dog"))
        self.assertFalse(campaign._catalog_clip_cached_hint(clip, self.work))

    def test_provider_catalog_only_flag_is_opt_in(self):
        with patch.object(nymeria, "automatic_candidates", return_value=[]) as candidates:
            campaign._nymeria_windows("Gardening", 300, 1800, catalog_only=True)
            self.assertTrue(candidates.call_args.kwargs["catalog_only"])
            campaign._nymeria_windows("Gardening", 300, 1800)
            self.assertNotIn("catalog_only", candidates.call_args.kwargs)
