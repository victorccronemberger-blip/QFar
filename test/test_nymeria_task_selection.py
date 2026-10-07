from __future__ import annotations

import copy
import csv
import json
import os
import socket
import sys
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

    def sdk_fixture(self, extraction):
        base = self.root / extraction / "projectaria_tools"
        for name in nymeria._SDK_WRAPPERS:
            path = base / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"SDK wrapper {name}\n".encode())
        native = base.parent / "_core_pybinds.pyd"
        native.write_bytes(b"native-sdk-v1")
        dll = base / "vendor/clock.dll"
        dll.parent.mkdir()
        dll.write_bytes(b"clock-sdk-v1")
        modules = {"projectaria_tools": SimpleNamespace(__file__=str(base / "__init__.py")),
                   "_core_pybinds": SimpleNamespace(__file__=str(native))}
        return modules, native, dll

    def test_sdk_evidence_survives_identical_mei_relocation(self):
        first, _native, _dll = self.sdk_fixture("_MEI_first_start")
        second, _native, _dll = self.sdk_fixture("_MEI_second_start")
        with patch.object(nymeria_vrs, "_bootstrap_projectaria", return_value=None), \
             patch.object(nymeria.importlib_metadata, "version", return_value="1.7.1"):
            with patch.dict(sys.modules, first):
                clip = json.loads(json.dumps(self.candidates()[0]))
                self.assertEqual(nymeria.revalidate_candidate(clip), clip)
            self.assertNotIn("_MEI", json.dumps(clip["selection_evidence"]["sdk_signature"]))
            nymeria.clear_caches()
            with patch.dict(sys.modules, second):
                self.assertEqual(nymeria.revalidate_candidate(clip), clip)

    def test_sdk_byte_changes_reject_saved_evidence_even_with_preserved_stats(self):
        modules, native, dll = self.sdk_fixture("_MEI_sdk_change")
        with patch.object(nymeria_vrs, "_bootstrap_projectaria", return_value=None), \
             patch.object(nymeria.importlib_metadata, "version", return_value="1.7.1"), \
             patch.dict(sys.modules, modules):
            for path in (native, dll):
                with self.subTest(binary=path.name):
                    clip = self.candidates()[0]
                    original = path.read_bytes()
                    marker = nymeria._stat(path)
                    stat_function = nymeria._stat
                    path.write_bytes(original.replace(b"v1", b"v2"))
                    # Fresh validation must verify the SDK's bytes even when
                    # every physical cache marker appears unchanged.
                    with patch.object(nymeria, "_stat", side_effect=lambda candidate:
                            marker if candidate == path else stat_function(candidate)), \
                         self.assertRaises(ValueError):
                        nymeria.revalidate_candidate(clip)
                    path.write_bytes(original)
                    nymeria.clear_caches()

    def test_irrelevant_sdk_module_import_does_not_change_evidence(self):
        modules, _native, _dll = self.sdk_fixture("_MEI_extra_import")
        extra = self.root / "unrelated-sdk-module.py"
        extra.write_bytes(b"unrelated SDK module")
        with patch.object(nymeria_vrs, "_bootstrap_projectaria", return_value=None), \
             patch.object(nymeria.importlib_metadata, "version", return_value="1.7.1"), \
             patch.dict(sys.modules, modules):
            clip = self.candidates()[0]
            with patch.dict(sys.modules, {"projectaria_tools.unrelated":
                    SimpleNamespace(__file__=str(extra))}):
                self.assertEqual(nymeria.revalidate_candidate(clip), clip)

    def test_measured_atomic_actions_match_only_the_requested_task(self):
        clips = self.candidates()
        self.assertTrue(clips)
        self.assertEqual(self.candidates("Gardening"), [])
        self.assertEqual(nymeria.automatic_candidates(), [])
        self.assertEqual(nymeria.revalidate_candidate(clips[0], task_name=FOLD,
            task_id="task-fold", registry_key="minute|task-fold|Fold"), clips[0])
        self.assertLess(clips[0]["dur_s"], 300)
        self.assertEqual(clips[0]["selection_evidence"]["task"]["id"], "task-fold")

    def test_nymeria_housekeeping_vacuum_needs_car_context_but_ego_scene_is_preserved(self):
        from moneymin import task_matching
        name = "Cleaning Out Car"
        self.write_rows("C vacuums the floor in the hallway while standing and walking.")
        self.assertEqual(self.candidates(name), [])
        self.write_rows("C vacuums the car seat and removes trash from the car interior while standing.")
        self.assertTrue(self.candidates(name))
        # The source-specific context requirement does not mutate the Ego4D
        # rule whose human car-washing scenario supplies the car context.
        ego_rule = task_matching.rule_for(name)
        caption = "C vacuums the seat and removes trash from the floor mat while standing."
        self.assertIsNotNone(task_matching.score_scenarios(ego_rule, ["Car/scooter washing"]))
        self.assertIsNotNone(task_matching.score_action(ego_rule, caption, [caption] * 12))

    def test_explicit_clothing_folds_are_valid_but_cloth_and_sewing_are_not(self):
        for text, accepted in (("C is folding a piece of clothing while standing.", True),
                               ("C is folding a cloth while standing.", False),
                               ("C is folding clothes while sewing their edges.", False)):
            with self.subTest(text=text):
                self.write_rows(text)
                self.assertEqual(bool(self.candidates()), accepted)

    def test_nymeria_requires_task_action_and_location_instead_of_background_objects(self):
        cases = (
            (FOLD, "C collects and piles clothes hangers from the closet rod while standing.", False),
            (FOLD, "C puts a shirt on a clothes hanger and hangs it inside the closet while standing.", True),
            (FOLD, "C is folding clothes with both hands while standing.", True),
            ("Organize the Garage", "C looks inside a utensil crock on shelves in the kitchen storage room while standing.", False),
            ("Organize the Garage", "C arranges utensils on shelves in the kitchen storage room while standing.", True),
            ("Organize the Garage", "C organizes tools on the shelves in the garage while standing.", True),
            ("Stack firewood", "C stacks wooden blocks from the Jenga game on the table while standing.", False),
            ("Stack firewood", "C stacks firewood logs into a pile in the backyard while standing.", True),
            ("Watering Outdoor Plants", "C holds a water bottle and checks a plant beside the bedroom table while standing.", False),
            ("Watering Outdoor Plants", "C holds a watering can beside the plants in the garden while standing.", False),
            ("Watering Outdoor Plants", "C pours water over the plants in the garden while standing.", True),
            ("Water Houseplants", "C holds a watering can beside the houseplants in the living room while standing.", False),
            ("Water Houseplants", "C waters the houseplants in the living room while standing.", True),
            ("Holiday Decoration Setup", "C removes Christmas decorations from the wall then puts the hanging decorations inside a box while standing.", True),
            ("Holiday Decoration Setup", "C looks at the decorations on the wall while standing.", False),
            ("Holiday Decoration Setup", "C hangs the Christmas decorations and attaches lights to the wall while standing.", True),
        )
        for name, caption, accepted in cases:
            with self.subTest(task=name, caption=caption):
                self.write_rows(caption)
                self.assertEqual(bool(self.candidates(name)), accepted)

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

    def test_dishwasher_workflow_keeps_real_rinsing_between_appliance_transfers(self):
        self.rgb = tuple(range(1_000_000_000_000, 1_500_000_000_001, 20_000_000))
        self.imu = tuple(range(1_000_000_000_000, 1_500_000_000_001, 4_000_000))
        transfer = "C puts a dirty plate into the dishwasher rack while standing in the kitchen."
        rinse = "C rinses the spatula in her left hand with water from the kitchen sink."
        rows = [transfer if index % 2 == 0 else rinse for index in range(85)]
        # A phase whose atomic interval ends outside the actual cut must not
        # prove completion. Keep this start fully within the final window.
        rows[-3] = "C starts the dishwasher after loading the dirty plates."
        rows[-2:] = ["C closes the dishwasher after loading the dirty plates."] * 2
        self._write_workflow(rows)
        clips = nymeria.automatic_candidates(task_name="Using the Dishwasher", root=self.root,
            min_dur_s=300, max_dur_s=1800)
        self.assertTrue(clips)
        self.assertGreaterEqual(clips[0]["dur_s"], 300)
        # Catalog expansion and the measured selector must agree about the
        # action; catalog evidence alone still cannot certify the IMU.
        from moneymin import nymeria_library
        raw, _hashes = nymeria._annotation_rows(self.seq)
        potential = nymeria_library._catalog_windows(raw, ["Using the Dishwasher"], 300, 1800)
        self.assertEqual(len(potential), len(clips))
        self.assertAlmostEqual(potential[0]["duration_s"], clips[0]["dur_s"])

    def test_dishwasher_completion_gate_obeys_phase_order_actor_and_cut_boundaries(self):
        name = "Using the Dishwasher"
        load = "C puts dirty plates into the dishwasher rack while standing."
        start = "C starts the dishwasher while standing."
        unload = "C removes clean plates from the dishwasher rack while standing."
        store = "C puts clean plates into the kitchen cupboard while standing."
        generic_button = (
            "C is standing by the dishwasher, pointing at the dishwasher with her right hand "
            "then pushes the button of the dishwasher with her right hand twice.")
        cases = (
            ([(0, 5, load), (10, 15, start)], 0, 15, True),
            ([(0, 5, unload), (10, 15, store)], 0, 15, True),
            ([(0, 5, load)], 0, 20, False),
            ([(0, 5, unload)], 0, 20, False),
            ([(0, 5, load), (10, 15, generic_button)], 0, 15, False),
            ([(0, 5, start), (10, 15, load)], 0, 15, False),
            ([(0, 5, store), (10, 15, unload)], 0, 15, False),
            ([(0, 5, load), (10, 15, start), (16, 20, load)], 0, 20, False),
            ([(0, 5, load), (10, 15, start)], 0, 14, False),
            ([(0, 5, load), (10, 15, start)], 1, 15, False),
            ([(0, 5, load), (10, 15, start)], 0, 10, False),
            ([(0, 5, load), (10, 15, "#O starts the dishwasher.")], 0, 15, False),
            ([(0, 5, load), (10, 15, "C stands by the dishwasher while her peer starts the dishwasher.")], 0, 15, False),
            ([(0, 5, load), (10, 15, "C watches her peer as she starts the dishwasher.")], 0, 15, False),
            ([(0, 5, load), (10, 15, "C tells her peer to start the dishwasher.")], 0, 15, False),
            ([(0, 5, load), (10, 15, "C starts the dishwasher while her peer stands nearby.")], 0, 15, True),
            ([(0, 5, "#O puts dirty plates into the dishwasher."), (10, 15, start)], 0, 15, False),
            ([(0, 5, unload), (10, 15, "C puts the phone into the cupboard.")], 0, 15, False),
            ([(0, 5, "C washes dirty plates beside the dishwasher."), (10, 15, start)], 0, 15, False),
            ([(0, 5, load), (10, 15, "C starts the coffee machine beside the dishwasher.")], 0, 15, False),
            ([(0, 5, load), (10, 15, "C presses the start button of the dishwasher.")], 0, 15, True),
            ([(0, 5, "C removes dirty plates from the dishwasher."), (10, 15, store)], 0, 15, False),
            ([(0, 5, unload), (10, 15, "C puts dirty plates into the cupboard.")], 0, 15, False),
            ([(0, 5, "C unloads the dishwasher of dirty plates."), (10, 15, store)], 0, 15, False),
        )
        for rows, left, right, accepted in cases:
            with self.subTest(rows=rows, start=left, end=right):
                self.assertEqual(nymeria.selection_window_complete(name, rows, left, right), accepted)

    def test_dishwasher_catalog_and_sdk_reject_loading_only_and_generic_button(self):
        from moneymin import nymeria_library
        self.rgb = tuple(range(1_000_000_000_000, 1_500_000_000_001, 20_000_000))
        self.imu = tuple(range(1_000_000_000_000, 1_500_000_000_001, 4_000_000))
        load = "C puts dirty plates into the dishwasher rack while standing."
        for last in (load, "C pushes the button of the dishwasher twice."):
            self._write_workflow([load] * 82 + [last] * 3)
            raw, _hashes = nymeria._annotation_rows(self.seq)
            with self.subTest(final_phase=last):
                self.assertEqual(nymeria_library._catalog_windows(raw, ["Using the Dishwasher"], 300, 1800), [])
                self.assertEqual(nymeria.automatic_candidates(task_name="Using the Dishwasher", root=self.root,
                    min_dur_s=300, max_dur_s=1800), [])

    def test_dishwasher_unload_and_put_away_is_valid_in_catalog_and_measured_selector(self):
        from moneymin import nymeria_library
        self.rgb = tuple(range(1_000_000_000_000, 1_500_000_000_001, 20_000_000))
        self.imu = tuple(range(1_000_000_000_000, 1_500_000_000_001, 4_000_000))
        text = ("C removes clean plates from the dishwasher rack and puts clean plates "
                "into the kitchen cupboard while standing.")
        self._write_workflow([text] * 85)
        raw, _hashes = nymeria._annotation_rows(self.seq)
        potential = nymeria_library._catalog_windows(raw, ["Using the Dishwasher"], 300, 1800)
        clips = nymeria.automatic_candidates(task_name="Using the Dishwasher", root=self.root,
            min_dur_s=300, max_dur_s=1800)
        self.assertTrue(potential)
        self.assertEqual(len(potential), len(clips))
        self.assertAlmostEqual(potential[0]["duration_s"], clips[0]["dur_s"])

    def test_dirty_unloading_is_rejected_without_requiring_clean_adjective_everywhere(self):
        from moneymin import nymeria_library
        self.rgb = tuple(range(1_000_000_000_000, 1_500_000_000_001, 20_000_000))
        self.imu = tuple(range(1_000_000_000_000, 1_500_000_000_001, 4_000_000))
        for adjective, accepted in (("dirty ", False), ("clean ", True), ("", True)):
            text = (f"C removes {adjective}plates from the dishwasher rack and puts "
                    f"{adjective}plates into the kitchen cupboard while standing.")
            self._write_workflow([text] * 85)
            raw, _hashes = nymeria._annotation_rows(self.seq)
            with self.subTest(adjective=adjective):
                self.assertEqual(bool(nymeria_library._catalog_windows(
                    raw, ["Using the Dishwasher"], 300, 1800)), accepted)
                self.assertEqual(bool(nymeria.automatic_candidates(task_name="Using the Dishwasher", root=self.root,
                    min_dur_s=300, max_dur_s=1800)), accepted)

    def test_v3_partial_selection_evidence_is_rejected_after_phase_gate_update(self):
        self.rgb = tuple(range(1_000_000_000_000, 1_500_000_000_001, 20_000_000))
        self.imu = tuple(range(1_000_000_000_000, 1_500_000_000_001, 4_000_000))
        self._write_workflow(["C puts dirty plates into the dishwasher rack while standing."] * 85)
        with patch.object(nymeria, "_ALGORITHM", "nymeria-atomic-device-v3"), \
             patch.object(nymeria, "selection_window_complete", return_value=True):
            old = nymeria.automatic_candidates(task_name="Using the Dishwasher", root=self.root,
                min_dur_s=300, max_dur_s=1800)[0]
        with self.assertRaisesRegex(ValueError, "evidência Nymeria atual ausente"):
            nymeria.revalidate_candidate(old)

    def _write_workflow(self, texts):
        with (self.narration / "atomic_action.csv").open("w", encoding="utf8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["start_time", "end_time", "Describe my atomic actions"])
            writer.writeheader()
            for index, text in enumerate(texts):
                writer.writerow({"start_time": 1000 + index * 5, "end_time": 1005 + index * 5,
                                 "Describe my atomic actions": text})
        nymeria.clear_caches()

    def test_dishwasher_workflow_rejects_sink_only_or_appliance_merely_nearby(self):
        from moneymin import nymeria_library
        for text in ("C washes a plate with water at the kitchen sink.",
                     "C washes a plate in the kitchen sink beside the dishwasher."):
            with self.subTest(text=text):
                rows = [(1000 + i * 5, 1005 + i * 5, text) for i in range(85)]
                self.assertEqual(nymeria_library._catalog_windows(
                    rows, ["Using the Dishwasher"], 300, 1800), [])

    def test_dishwasher_workflow_does_not_cross_hygiene_real_garden_or_sensor_gaps(self):
        from moneymin import nymeria_library
        transfer = "C puts a dirty plate into the dishwasher rack while standing in the kitchen."
        for boundary in ("C sits down and uses her phone.", GARDEN_TEXT):
            rows = [(1000 + i * 5, 1005 + i * 5,
                     boundary if i == 42 else transfer) for i in range(85)]
            with self.subTest(boundary=boundary):
                self.assertEqual(nymeria_library._catalog_windows(
                    rows, ["Using the Dishwasher"], 300, 1800), [])
        unrelated = [(1000 + i * 5, 1005 + i * 5,
                      "C reads a book while standing in the living room." if 30 <= i <= 48 else transfer)
                     for i in range(85)]
        self.assertEqual(nymeria_library._catalog_windows(
            unrelated, ["Using the Dishwasher"], 300, 1800), [])
        self.rgb = tuple(range(1_000_000_000_000, 1_500_000_000_001, 20_000_000))
        self.imu = tuple(t for t in range(1_000_000_000_000, 1_500_000_000_001, 4_000_000)
                         if t < 1_200_000_000_000 or t > 1_220_000_000_000)
        self._write_workflow([transfer] * 85)
        self.assertEqual(nymeria.automatic_candidates(task_name="Using the Dishwasher", root=self.root,
            min_dur_s=300, max_dur_s=1800), [])

    def test_broad_yard_rival_needs_an_actual_yard_action(self):
        from moneymin import task_matching
        for name in ("Gardening", "Full Yard Maintenance"):
            rule = nymeria.selection_rule_for(name)
            with self.subTest(task=name):
                prepared = task_matching.prepare_span_events([
                    (0, "C washes a spatula with water in the kitchen sink."),
                    (5, "C pours water onto plants in the garden."),
                    (10, "C mows the lawn while standing."),
                ])
                labels = task_matching.label_span_events(prepared, [(name, rule)])
                self.assertFalse(labels[0])
                self.assertIn(name, labels[1])
                self.assertIn(name, labels[2])
        self.assertTrue(nymeria.selection_activity_mode("Using the Dishwasher"))
        self.assertFalse(nymeria.selection_activity_mode("Brew Coffee or Tea"))

    def test_real_frank_dishwasher_captions_keep_the_named_appliance_action(self):
        from moneymin import task_matching
        # Atomic annotations from 20231122_s0_frank_hayden_act2_rjtf5a;
        # no source recording or account identity is needed in this regression.
        texts = [
            "C is leaning towards the dishwasher while putting the spatula on the dishwasher's rack with her left hand, straightens her back then steps towards the kitchen sink.",
            "C is leaning forward in the kitchen as she puts down the colander on the dishwasher rack using both of her hands and then grabs a plate using both hands",
            "C is leaning forward in the kitchen as she arranges the kitchenware on the dishwasher rack using both of her hands",
            "C is standing by the dishwasher, pointing at the dishwasher with her right hand then pushes the button of the dishwasher with her right hand twice.",
        ]
        prepared = task_matching.prepare_span_events(enumerate(texts))
        labels = task_matching.label_span_events(prepared, nymeria.selection_rules().items())
        for text, names in zip(texts, labels):
            with self.subTest(text=text):
                self.assertIn("Using the Dishwasher", names)
                self.assertNotIn("Gardening", names)
                self.assertNotIn("Full Yard Maintenance", names)

    def test_clean_appliance_boundary_binds_cleaning_to_the_appliance(self):
        from moneymin import task_matching
        rule = nymeria.selection_rule_for("Clean Appliance")
        texts = [
            "C is standing in the kitchen as she picks up the pan from the stove using her left hand and then rinses it on the sink as she opens and closes the faucet using her right hand",
            "C scrubs the oven while standing in the kitchen.",
            "C wipes the kitchen stove with a sponge while standing.",
            "C rinses the dishwasher while standing in the kitchen.",
        ]
        labels = task_matching.label_span_events(task_matching.prepare_span_events(enumerate(texts)),
                                                 [("Clean Appliance", rule)])
        self.assertFalse(labels[0])
        for result in labels[1:]:
            self.assertIn("Clean Appliance", result)

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
