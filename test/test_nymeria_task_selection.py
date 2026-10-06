from __future__ import annotations

import copy
import csv
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from moneymin import campaign, nymeria, nymeria_vrs, readiness
from moneymin.campaign_types import CampaignConfig, TaskSpec

FOLD = "Folding Clothes or Putting Them on Hangers"
FOLD_TEXT = "C is folding a shirt with both hands while standing in the living room."
GARDEN_TEXT = "C pulls weeds from the garden soil by hand while standing."


class NymeriaTaskSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.seq = self.root / "sequence"
        self.seq.mkdir()
        (self.seq / "metadata.json").write_text(json.dumps({
            "uid": "fixture", "script": "S11Laundary", "head_duration_sec": 9999}), "utf8")
        self.media = self.seq / "recording_head/data"
        self.media.mkdir(parents=True)
        for name in ("data.vrs", "motion.vrs"):
            (self.media / name).write_bytes(b"offline-vrs-placeholder")
        self.narration = self.seq / "narration"
        self.narration.mkdir()
        self.write_rows()
        self.rgb = tuple(range(1_000_000_000_000, 1_300_000_000_001, 20_000_000))
        self.imu = tuple(range(1_000_000_000_000, 1_300_000_000_001, 4_000_000))
        self.patches = [
            patch.dict(os.environ, {"NYMERIA_ROOT": str(self.root)}),
            patch.object(nymeria_vrs, "_provider", return_value=self),
            patch.object(nymeria_vrs, "_stream_timestamps", side_effect=self.timestamps),
            patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")),
        ]
        for entry in self.patches:
            entry.start()
            self.addCleanup(entry.stop)
        nymeria.clear_caches()
        self.addCleanup(nymeria.clear_caches)

    def get_stream_id_from_label(self, label):
        return label

    def get_metadata(self):
        return SimpleNamespace(device_serial="offline-fixture-head")

    def timestamps(self, _provider, stream):
        return self.rgb if stream == "camera-rgb" else self.imu

    def write_rows(self, text=FOLD_TEXT, *, origin=1000, count=41):
        with (self.narration / "atomic_action.csv").open("w", encoding="utf8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "request_id", "gaia_id", "start_time", "end_time", "annotator",
                "creation_time", "Describe my atomic actions"])
            writer.writeheader()
            for index in range(count):
                writer.writerow({"start_time": origin + index * 5,
                    "end_time": origin + index * 5 + 5,
                    "Describe my atomic actions": text})

    def candidates(self, name=FOLD, **kwargs):
        return nymeria.automatic_candidates(task_name=name, task_id="task-fold",
            registry_key="minute|task-fold|Fold", min_dur_s=60, max_dur_s=240, **kwargs)

    def test_measured_atomic_actions_match_only_the_requested_task(self):
        clips = self.candidates()
        self.assertTrue(clips)
        self.assertEqual(self.candidates("Gardening"), [])
        self.assertEqual(nymeria.automatic_candidates(), [])
        self.assertEqual(nymeria.revalidate_candidate(clips[0], task_name=FOLD,
            task_id="task-fold", registry_key="minute|task-fold|Fold"), clips[0])
        self.assertLess(clips[0]["dur_s"], 300)
        self.assertEqual(clips[0]["selection_evidence"]["task"]["id"], "task-fold")

    def test_explicit_clothing_folds_are_valid_but_cloth_and_sewing_are_not(self):
        for text, accepted in (("C is folding a piece of clothing while standing.", True),
                               ("C is folding a cloth while standing.", False),
                               ("C is folding clothes while sewing their edges.", False)):
            with self.subTest(text=text):
                self.write_rows(text)
                self.assertEqual(bool(self.candidates()), accepted)

    def test_catalog_only_and_sdk_failure_are_not_candidates(self):
        (self.media / "data.vrs").unlink()
        self.assertEqual(self.candidates(), [])
        seq = nymeria.list_sequences()[0]
        self.assertEqual(seq["duration_s"], 0)
        self.assertFalse(seq["selection_ready"])
        (self.media / "data.vrs").write_bytes(b"invalid")
        with patch.object(nymeria_vrs, "_provider", side_effect=ImportError("SDK unavailable")):
            self.assertEqual(self.candidates(), [])

    def test_root_is_media_library_unless_explicitly_overridden(self):
        with patch.dict(os.environ, {"NYMERIA_ROOT": ""}), \
             patch.object(nymeria.config, "MEDIA_DATA_DIR", self.root / "library"):
            self.assertEqual(nymeria.data_root(), (self.root / "library/nymeria").resolve())
        self.assertEqual(nymeria.data_root(), self.root.resolve())

    def test_same_stat_annotation_change_is_rejected_by_fresh_effect_gate(self):
        original = self.candidates()[0]
        csv_path = self.narration / "atomic_action.csv"
        saved_stat = csv_path.stat()
        content = csv_path.read_bytes()
        replacement = content.replace(b"folding", b"holding")
        self.assertEqual(len(content), len(replacement))
        csv_path.write_bytes(replacement)
        os.utime(csv_path, ns=(saved_stat.st_atime_ns, saved_stat.st_mtime_ns))
        with self.assertRaises(ValueError):
            nymeria.revalidate_candidate(original)

    def test_source_and_annotation_changes_invalidate_readonly_pool(self):
        self.assertTrue(self.candidates())
        self.write_rows(GARDEN_TEXT)
        self.assertEqual(self.candidates(), [])
        self.assertTrue(self.candidates("Gardening"))
        self.imu = (1_000_000_000_000, 1_000_004_000_000)
        (self.media / "motion.vrs").write_bytes(b"changed-media")
        self.assertEqual(self.candidates("Gardening"), [])

    def test_manual_identity_and_window_cannot_override_task_evidence(self):
        clip = self.candidates()[0]
        for overrides in ({"task_name": "Gardening"}, {"task_id": "other"},
                          {"registry_key": "minute|other|Fold"}):
            with self.subTest(overrides=overrides), \
                 patch.object(nymeria_vrs, "_provider", side_effect=AssertionError("task mismatch before provider")), \
                 self.assertRaises(ValueError):
                nymeria.revalidate_candidate(clip, **overrides)
        tampered = copy.deepcopy(clip)
        tampered["window_s"][1] += 1
        with self.assertRaises(ValueError):
            nymeria.revalidate_candidate(tampered)
        self.assertFalse(campaign._prepare_queue_accepts(clip, "Gardening"))
        self.assertFalse(campaign._prepare_queue_accepts({"source": "nymeria",
            "clip_uid": clip["clip_uid"]}, FOLD))

    def test_no_silent_clamping_and_no_guessed_relative_annotation_clock(self):
        for start, end in ((-1, 60), (0, 301), (80, 60), (1, 1), (0, float("nan"))):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                nymeria.device_window_for_sequence(self.seq, start_s=start, end_s=end)
        self.write_rows(origin=0)
        self.assertEqual(self.candidates(), [])

    def test_different_head_devices_or_unassociated_csv_clock_are_rejected(self):
        other = SimpleNamespace(get_metadata=lambda: SimpleNamespace(device_serial="other-head"),
                                get_stream_id_from_label=lambda label: label)
        with patch.object(nymeria_vrs, "_provider", side_effect=[self, other]):
            self.assertEqual(self.candidates(), [])
        self.write_rows(origin=1000.001)
        self.assertEqual(self.candidates(), [])

    def test_sensor_gaps_and_competing_tasks_end_action_windows(self):
        self.imu = tuple(t for t in self.imu if t < 1_050_000_000_000 or t > 1_180_000_000_000)
        self.assertEqual(self.candidates(), [])

        nymeria.clear_caches()
        self.imu = tuple(range(1_000_000_000_000, 1_300_000_000_001, 4_000_000))
        with (self.narration / "atomic_action.csv").open("w", encoding="utf8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["start_time", "end_time", "Describe my atomic actions"])
            writer.writeheader()
            for index in range(41):
                writer.writerow({"start_time": 1000 + index * 5, "end_time": 1005 + index * 5,
                    "Describe my atomic actions": GARDEN_TEXT if index % 9 == 8 else FOLD_TEXT})
        self.assertEqual(self.candidates(), [])

    def test_full_yard_maintenance_keeps_its_existing_five_minute_minimum(self):
        self.write_rows("C mows the lawn, rakes grass clippings and pulls weeds from soil while standing.",
                        count=21)
        # The narration proves its several actions; its 105s duration cannot
        # satisfy the current Full Yard Maintenance rule's 300s requirement.
        self.assertEqual(self.candidates("Full Yard Maintenance"), [])

    def test_automatic_manual_pool_all_and_ambos_use_same_category(self):
        task = TaskSpec("task-fold", "Cleaning/laundry", 60, 240, task_name=FOLD)
        cfg = CampaignConfig(accounts=[], tasks=[task], work_dir=self.root, dataset_provider="nymeria",
                             content_mode="dataset")
        clips = campaign.automatic_candidates(task, cfg)
        self.assertTrue(clips)
        self.assertEqual(clips[0]["selection_evidence"]["task"]["id"], task.task_id)
        with patch.object(campaign, "_ranked_pools", return_value={}), \
             patch.object(campaign, "_task_candidates", return_value=[]), \
             patch.object(campaign.holoassist, "list_clips", return_value=[]):
            for provider in ("nymeria", "all", "ambos"):
                with self.subTest(provider=provider):
                    self.assertTrue(campaign._compatible_task_clips(FOLD, provider))
                    self.assertFalse(campaign._compatible_task_clips("Gardening", provider))

    def test_repeated_poll_reuses_measured_sdk_indexes(self):
        nymeria.clear_caches()
        with patch.object(nymeria_vrs, "_provider", return_value=self) as provider:
            self.assertTrue(self.candidates())
            self.assertTrue(self.candidates())
            self.assertEqual(provider.call_count, 2)

    def test_frozen_matcher_without_an_extracted_source_file_remains_usable(self):
        with patch.object(nymeria.task_matching, "__file__", str(self.root / "PYZ/matcher.pyc")):
            clips = self.candidates()
            self.assertTrue(clips)
            self.assertEqual(nymeria.revalidate_candidate(clips[0]), clips[0])

    def test_readiness_separates_sdk_from_measured_library(self):
        with patch.object(readiness, "_binary_works", return_value=True), \
             patch.object(readiness, "_valid_account_tokens", return_value=(1, 1)), \
             patch.object(nymeria_vrs, "_bootstrap_projectaria", return_value=None):
            result = readiness.campaign_readiness("nymeria")
            checks = {check["name"]: check for check in result["checks"]}
            self.assertEqual(checks["SDK Nymeria"]["status"], "ok")
            self.assertEqual(checks["Biblioteca Nymeria"]["status"], "ok")
            (self.media / "motion.vrs").unlink()
            result = readiness.campaign_readiness("nymeria")
            checks = {check["name"]: check for check in result["checks"]}
            self.assertEqual(checks["SDK Nymeria"]["status"], "ok")
            self.assertEqual(checks["Biblioteca Nymeria"]["status"], "error")
            self.assertFalse(result["ready"])


if __name__ == "__main__":
    unittest.main()
