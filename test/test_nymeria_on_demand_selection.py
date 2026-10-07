"""Annotation queues cannot bypass the measured Nymeria send contract."""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from moneymin import nymeria, nymeria_library, nymeria_vrs, readiness

TASK = "Folding Clothes or Putting Them on Hangers"
ACTION = "C is folding a shirt with both hands while standing in the living room."


class NymeriaOnDemandSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.seq = self.root / "sequence"
        self.seq.mkdir()
        metadata = json.dumps({"uid": "fixture", "head_duration_sec": 9999}).encode()
        (self.seq / "metadata.json").write_bytes(metadata)
        self.narration = self.seq / "narration"
        self.narration.mkdir()
        self.write_rows()
        asset = lambda name, data: {"filename": name, "sha1sum": hashlib.sha1(data).hexdigest(),
            "file_size_bytes": len(data), "download_url": "https://fixture.fbcdn.net/" + name}
        self.manifest = {"sequences": {"sequence": {
            "metadata_json": asset("metadata.json", metadata),
            "narration": asset("narration.zip", b"annotation archive fixture"),
            "timesync_and_imu": asset("imu.zip", b"imu archive fixture"),
            "recording_head_data_data_vrs": asset("data.vrs", b"rgb fixture")}}}
        path = self.root / "manifest.json"
        path.write_text(json.dumps(self.manifest), "utf8")
        nymeria_library.import_manifest(path, self.root)
        self.rgb = tuple(range(1_000_000_000_000, 1_460_000_000_001, 20_000_000))
        self.imu = tuple(range(1_000_000_000_000, 1_460_000_000_001, 4_000_000))
        for entry in (
            patch.dict(os.environ, {"NYMERIA_ROOT": str(self.root)}),
            patch.object(nymeria_vrs, "_provider", return_value=self),
            patch.object(nymeria_vrs, "_stream_timestamps", side_effect=self.timestamps),
            patch.object(nymeria, "_sdk_signature", return_value=(("fixture-sdk", ("1",)),)),
            patch.object(nymeria_library.requests, "Session", side_effect=AssertionError("network forbidden")),
        ):
            entry.start()
            self.addCleanup(entry.stop)
        nymeria.clear_caches()
        self.addCleanup(nymeria.clear_caches)

    def get_stream_id_from_label(self, label):
        return label

    def get_metadata(self):
        return SimpleNamespace(device_serial="fixture-head")

    def timestamps(self, _provider, stream):
        return self.rgb if stream == "camera-rgb" else self.imu

    def write_rows(self, text=ACTION, *, origin=1000):
        with (self.narration / "atomic_action.csv").open("w", newline="", encoding="utf8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["start_time", "end_time", "Describe my atomic actions"])
            writer.writeheader()
            for index in range(91):
                writer.writerow({"start_time": origin + index * 5, "end_time": origin + 5 + index * 5,
                                 "Describe my atomic actions": text})

    def plans(self, name=TASK):
        return nymeria.planned_candidates(task_name=name, task_id="task",
            registry_key="minute|task|Fixture", min_dur_s=300, max_dur_s=1800, root=self.root)

    def acquire_fixture(self):
        directory = self.seq / "recording_head/data"
        directory.mkdir(parents=True)
        (directory / "data.vrs").write_bytes(b"rgb fixture")
        (directory / "motion.vrs").write_bytes(b"imu fixture")

    def test_catalog_queue_neither_measures_nor_claims_ready(self):
        with patch.object(nymeria, "_snapshot", side_effect=AssertionError("must not open SDK")):
            plans = self.plans()
            self.assertTrue(plans)
            plan = plans[0]
            self.assertTrue(plan["acquisition_required"])
            self.assertFalse(plan["selection_ready"])
            self.assertEqual(plan["capacity_kind"], "annotation_estimate")
            self.assertEqual(plan["selection_evidence"]["sensor_coverage"], "unmeasured")
            self.assertEqual(nymeria.revalidate_planned_candidate(plan, root=self.root), plan)
            automatic = nymeria.automatic_candidates(task_name=TASK, min_dur_s=300,
                max_dur_s=1800, root=self.root, include_planned=True)
            self.assertEqual(len(automatic), len(plans))
            self.assertTrue(all(row["acquisition_required"] for row in automatic))
        self.assertFalse((self.seq / "recording_head/data/data.vrs").exists())
        self.assertNotIn("download_url", json.dumps(plan))
        with self.assertRaises(ValueError):
            nymeria.revalidate_candidate(plan)

    def test_resolver_requires_acquisition_and_then_same_sdk_window(self):
        plan = self.plans()[0]
        with self.assertRaises(ValueError):
            nymeria.resolve_planned_candidate(plan, root=self.root)
        self.acquire_fixture()
        actual = nymeria.resolve_planned_candidate(plan, root=self.root)
        self.assertTrue(actual["clip_uid"].startswith("nymeria:"))
        self.assertNotEqual(actual["clip_uid"], plan["clip_uid"])
        self.assertEqual(actual["acquired_from_planned_clip_uid"], plan["clip_uid"])
        self.assertEqual(actual["dedup_clip_uids"], [plan["clip_uid"]])
        self.assertEqual(actual["device_window_ns"], plan["planned_device_window_ns"])
        self.assertEqual(actual["selection_evidence"]["algorithm"], "nymeria-atomic-device-v4")
        roundtrip = nymeria.revalidate_candidate(actual)
        self.assertEqual(roundtrip["acquired_from_planned_clip_uid"], plan["clip_uid"])
        self.assertEqual(roundtrip["dedup_clip_uids"], [plan["clip_uid"]])

    def test_first_annotation_after_sdk_origin_resolves_exact_absolute_cut(self):
        self.write_rows(origin=1010)
        self.rgb = tuple(range(1_000_000_000_000, 1_470_000_000_001, 20_000_000))
        self.imu = tuple(range(1_000_000_000_000, 1_470_000_000_001, 4_000_000))
        plan = self.plans()[0]
        self.acquire_fixture()
        with patch.object(nymeria, "_windows", side_effect=AssertionError("exact planned cut is reclassified")):
            actual = nymeria.resolve_planned_candidate(plan, root=self.root)
            nymeria.revalidate_candidate(actual)
        self.assertGreater(actual["window_s"][0], plan["window_s"][0])
        self.assertEqual(actual["window_s"][0] - plan["window_s"][0], 10.0)
        self.assertEqual(actual["device_window_ns"], plan["planned_device_window_ns"])
        forged = {**copy.deepcopy(actual), "clip_uid": "nymeria:other:0:450"}
        with self.assertRaises(ValueError):
            nymeria.revalidate_candidate(forged)

    def test_measured_source_is_retained_without_remote_manifest(self):
        self.acquire_fixture()
        (self.root / "_catalog/download_urls.json").unlink()
        actual = nymeria.automatic_candidates(task_name=TASK, min_dur_s=300,
            max_dur_s=1800, root=self.root, include_planned=True)
        self.assertTrue(actual)
        self.assertTrue(all(row["clip_uid"].startswith("nymeria:") for row in actual))
        self.assertTrue(all(not row.get("acquisition_required") for row in actual))

    def test_plan_changes_task_window_annotations_or_assets_fail_closed(self):
        plan = self.plans()[0]
        for changes in ({"path": str(self.root)}, {"task_id": "other"},
                        {"planned_device_window_ns": [0, 1]}, {"selection_ready": True}):
            with self.subTest(changes=changes):
                altered = {**copy.deepcopy(plan), **changes}
                with self.assertRaises(ValueError):
                    nymeria.revalidate_planned_candidate(altered, root=self.root)
        self.write_rows(ACTION + " More detail.")
        with self.assertRaises(ValueError):
            nymeria.revalidate_planned_candidate(plan, root=self.root)
        self.write_rows()
        self.manifest["sequences"]["sequence"]["recording_head_data_data_vrs"]["sha1sum"] = "a" * 40
        source = self.root / "changed.json"
        source.write_text(json.dumps(self.manifest), "utf8")
        nymeria_library.import_manifest(source, self.root)
        with self.assertRaises(ValueError):
            nymeria.revalidate_planned_candidate(plan, root=self.root)

    def test_incomplete_dishwasher_workflow_never_becomes_download_candidate(self):
        self.write_rows("C puts a dirty plate into the dishwasher rack while standing.")
        self.assertEqual(self.plans("Using the Dishwasher"), [])

    def test_sdk_clock_or_sensor_gap_cannot_promote_estimate(self):
        for invalid in ("clock", "gap"):
            with self.subTest(invalid=invalid):
                plan = self.plans()[0]
                if not (self.seq / "recording_head/data/data.vrs").exists():
                    self.acquire_fixture()
                original_rgb, original_imu = self.rgb, self.imu
                try:
                    if invalid == "clock":
                        self.rgb = tuple(value + 2_000_000 for value in self.rgb)
                    else:
                        self.imu = tuple(value for value in self.imu
                                         if not 1_200_000_000_000 < value < 1_200_100_000_000)
                    with self.assertRaises(ValueError):
                        nymeria.resolve_planned_candidate(plan, root=self.root)
                finally:
                    self.rgb, self.imu = original_rgb, original_imu
                    nymeria.clear_caches()

    def test_readiness_allows_catalog_without_vrs_only_in_on_demand_mode(self):
        with patch.object(readiness, "_binary_works", return_value=True), \
             patch.object(readiness, "_private_browser_present", return_value=True), \
             patch.object(readiness, "_valid_account_tokens", return_value=(1, 1)), \
             patch.object(nymeria_vrs, "_bootstrap_projectaria", return_value=None), \
             patch.object(nymeria, "list_sequences", side_effect=AssertionError("no premeasurement")):
            result = readiness.campaign_readiness("nymeria")
        self.assertTrue(result["ready"])
        check = next(row for row in result["checks"] if row["name"] == "Biblioteca Nymeria")
        self.assertIn("quando usada", check["detail"])
        with patch.object(readiness, "_binary_works", return_value=True), \
             patch.object(readiness, "_valid_account_tokens", return_value=(1, 1)), \
             patch.object(nymeria_vrs, "_bootstrap_projectaria", return_value=None), \
             patch.object(nymeria, "list_sequences", return_value=[]):
            self.assertFalse(readiness.campaign_readiness("nymeria", content_mode="cache")["ready"])


if __name__ == "__main__":
    unittest.main()
