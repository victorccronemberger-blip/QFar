"""Real atomic-action planning stays bounded across the category catalog.

VRS byte fixtures and the timestamp provider are inert. These tests never
download, prepare media, contact Minute or accept a plan as sensor evidence.
"""
from collections import Counter
import copy
from dataclasses import replace
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from moneymin import campaign, ego4d, nymeria, nymeria_library, nymeria_vrs, task_matching
import test_nymeria_on_demand_selection as _fixture

ACTION, TASK = _fixture.ACTION, _fixture.TASK


class NymeriaCatalogBatchTests(unittest.TestCase):
    # Reuse only the original real manifest/annotation/provider fixture methods,
    # without inheriting and rerunning its existing test methods.
    setUp = _fixture.NymeriaOnDemandSelectionTests.setUp
    write_rows = _fixture.NymeriaOnDemandSelectionTests.write_rows
    plans = _fixture.NymeriaOnDemandSelectionTests.plans
    acquire_fixture = _fixture.NymeriaOnDemandSelectionTests.acquire_fixture
    get_stream_id_from_label = _fixture.NymeriaOnDemandSelectionTests.get_stream_id_from_label
    get_metadata = _fixture.NymeriaOnDemandSelectionTests.get_metadata
    timestamps = _fixture.NymeriaOnDemandSelectionTests.timestamps

    def all_tasks(self):
        names = json.loads((Path(__file__).parent / "current_minute_task_names.json").read_text())
        return [{"id": f"task-{index}", "name": name} for index, name in enumerate(names)]

    def add_second_sequence(self):
        second = self.root / "sequence-garden"
        shutil.copytree(self.seq, second)
        annotation = second / "narration/atomic_action.csv"
        annotation.write_text(annotation.read_text(encoding="utf8").replace(
            ACTION, "C waters the outdoor garden plants with a hose while standing."), encoding="utf8")
        self.manifest["sequences"][second.name] = copy.deepcopy(
            self.manifest["sequences"][self.seq.name])
        source = self.root / "two-sequences.json"
        source.write_text(json.dumps(self.manifest), encoding="utf8")
        nymeria_library.import_manifest(source, self.root)
        return second

    def test_49_categories_two_ranges_read_and_plan_each_source_once_per_range(self):
        second = self.add_second_sequence()
        tasks = self.all_tasks()
        self.assertEqual(len(tasks), 49)
        counts = Counter()
        stats = Counter()
        original_open = Path.open
        original_stat = nymeria._stat

        def observed_open(path, *args, **kwargs):
            if path.suffix == ".vrs":
                raise AssertionError("category planning opened VRS")
            if path.is_relative_to(self.root):
                counts[path] += 1
            return original_open(path, *args, **kwargs)

        def observed_stat(path):
            value = Path(path)
            if value.is_relative_to(self.root):
                stats[value] += 1
            return original_stat(path)

        with patch.object(Path, "open", observed_open), \
             patch.object(nymeria, "_stat", side_effect=observed_stat), \
             patch.object(nymeria_library, "_catalog_windows",
                          wraps=nymeria_library._catalog_windows) as planner, \
             patch.object(nymeria, "_snapshot", side_effect=AssertionError("category measured a source")), \
             patch.object(nymeria, "_sdk_signature", side_effect=AssertionError("category hashed SDK")), \
             patch.object(nymeria_vrs, "_provider", side_effect=AssertionError("category opened SDK")), \
             ego4d.selection_operation():
            rows = []
            for minimum, maximum in ((60, 1800), (300, 1800)):
                for row in tasks:
                    rows.extend(nymeria.automatic_candidates(task_name=row["name"],
                        task_id=row["id"], registry_key=f"minute|{row['id']}|Fixture",
                        min_dur_s=minimum, max_dur_s=maximum, root=self.root,
                        include_planned=True, catalog_only=True))
            self.assertTrue(rows)
            self.assertTrue(all(row["acquisition_required"] for row in rows))
            self.assertTrue(all(not row["selection_ready"] for row in rows))
            self.assertEqual(planner.call_count, 4, "2 sources × 2 ranges, not 49 categories")
        for seq in (self.seq, second):
            for path in (seq / "metadata.json", seq / "narration/atomic_action.csv"):
                with self.subTest(path=path.name, source=seq.name):
                    self.assertEqual(counts[path], 1, "read once in the operation's immutable snapshot")
                    self.assertEqual(stats[path], 1, "stat inventory once, not once per task")
        self.assertEqual(counts[self.root / "_catalog/download_urls.json"], 1)

    def test_batch_plans_equal_fresh_per_category_plans_for_all_tasks_and_ranges(self):
        second = self.add_second_sequence()
        tasks = self.all_tasks()
        groups = nymeria_library._load(self.root)["sequences"]
        for minimum, maximum in ((60.0, 1800.0), (300.0, 1800.0)):
            with ego4d.selection_operation():
                for row in tasks:
                    name = task_matching.canonical_task_name(row["name"])
                    expected = []
                    for seq in (self.seq, second):
                        expected.extend(nymeria._planned_sequence(
                            seq, groups[seq.name], name, minimum, maximum))
                    expected = [nymeria.bind_task(clip, task_name=name, task_id=row["id"],
                        registry_key=f"minute|{row['id']}|Fixture")
                        for clip in sorted(expected, key=lambda clip: -clip["dur_s"])]
                    actual = nymeria.planned_candidates(task_name=row["name"],
                        task_id=row["id"], registry_key=f"minute|{row['id']}|Fixture",
                        min_dur_s=minimum, max_dur_s=maximum, root=self.root)
                    with self.subTest(task=name, minimum=minimum):
                        self.assertEqual(actual, expected)

    def test_available_tasks_real_provider_shares_operation_and_never_rebuilds_each_clip(self):
        self.add_second_sequence()
        with patch.object(nymeria_library, "_catalog_windows",
                          wraps=nymeria_library._catalog_windows) as planner, \
             patch.object(nymeria, "_snapshot", side_effect=AssertionError("category measured a source")), \
             patch.object(nymeria, "_sdk_signature", side_effect=AssertionError("category hashed SDK")), \
             patch.object(nymeria, "revalidate_planned_candidate",
                          side_effect=AssertionError("category rebuilt each window")), \
             patch.object(nymeria_vrs, "_provider", side_effect=AssertionError("category opened SDK")):
            rows = campaign.available_tasks("fixture@example.invalid", "fixture-org",
                remote_tasks=self.all_tasks(), dataset_provider="nymeria", content_mode="dataset",
                min_dur_s=300, max_dur_s=1800, include_unavailable=True)
            self.assertEqual(len(rows), 49)
            watering = next(row for row in rows if row["name"] == "Watering Outdoor Plants")
            self.assertGreater(watering["clip_count"], 0)
            self.assertTrue(watering["requires_measured_validation"])
            self.assertEqual(planner.call_count, 4, "2 sources × overall/requested duration ranges")

    def test_between_operations_annotations_metadata_manifest_rules_bounds_and_ids_invalidate(self):
        with ego4d.selection_operation():
            first = self.plans()
        self.assertTrue(first)
        original = first[0]
        self.write_rows(ACTION + " A second shirt is on the table.")
        with ego4d.selection_operation():
            annotated = self.plans()
        self.assertTrue(annotated)
        self.assertNotEqual(annotated[0]["selection_evidence"]["annotations_sha256"],
                            original["selection_evidence"]["annotations_sha256"])
        with self.assertRaises(ValueError):
            nymeria.revalidate_planned_candidate(original, root=self.root)
        metadata = self.seq / "metadata.json"
        metadata.write_text(json.dumps({"uid": "new-metadata", "head_duration_sec": 9999}), "utf8")
        with ego4d.selection_operation():
            updated_metadata = self.plans()
        self.assertEqual(updated_metadata[0]["uid"], "new-metadata")
        self.assertNotEqual(updated_metadata[0]["selection_evidence"]["metadata_sha256"],
                            annotated[0]["selection_evidence"]["metadata_sha256"])
        self.manifest["sequences"][self.seq.name]["recording_head_data_data_vrs"]["sha1sum"] = "a" * 40
        source = self.root / "changed-assets.json"
        source.write_text(json.dumps(self.manifest), "utf8")
        nymeria_library.import_manifest(source, self.root)
        with ego4d.selection_operation():
            updated_asset = self.plans()
        self.assertNotEqual(updated_asset[0]["selection_evidence"]["asset_identity"],
                            updated_metadata[0]["selection_evidence"]["asset_identity"])
        original_rule = nymeria.selection_rule_for
        stricter = replace(original_rule(TASK), min_span_s=600)
        with patch.object(nymeria, "selection_rule_for", side_effect=lambda name:
                          stricter if name == TASK else original_rule(name)), ego4d.selection_operation():
            self.assertEqual(self.plans(), [], "rules change cannot reuse the prior 450s plan")
        with ego4d.selection_operation():
            self.assertTrue(self.plans())
            self.assertEqual(nymeria.planned_candidates(task_name=TASK, min_dur_s=600,
                                                       max_dur_s=1800, root=self.root), [])
            bounded = nymeria.planned_candidates(task_name=TASK, task_id="other-task",
                registry_key="minute|other-task|Other", min_dur_s=60, max_dur_s=300, root=self.root)
            self.assertTrue(bounded)
            self.assertTrue(all(row["dur_s"] <= 300 for row in bounded))
            self.assertTrue(all(row["task_id"] == "other-task" for row in bounded))
            self.assertTrue(all(row["selection_evidence"]["task"]["registry_key"] ==
                                "minute|other-task|Other" for row in bounded))
            self.assertTrue(all(row["selection_evidence"]["duration_bounds_s"] ==
                                [60.0, 300.0] for row in bounded))

    def test_catalog_only_does_not_measure_legacy_sources_and_default_still_does(self):
        self.acquire_fixture()
        (self.root / "_catalog/download_urls.json").unlink()
        with patch.object(nymeria, "_snapshot", side_effect=AssertionError("catalog must not measure")), \
             patch.object(nymeria, "_sdk_signature", side_effect=AssertionError("catalog must not hash SDK")), \
             patch.object(nymeria_vrs, "_provider", side_effect=AssertionError("catalog must not open SDK")):
            self.assertEqual(nymeria.automatic_candidates(task_name=TASK, min_dur_s=300,
                max_dur_s=1800, root=self.root, include_planned=True, catalog_only=True), [])
        with patch.object(nymeria, "_snapshot", wraps=nymeria._snapshot) as measured:
            actual = nymeria.automatic_candidates(task_name=TASK, min_dur_s=300,
                max_dur_s=1800, root=self.root, include_planned=True)
            self.assertTrue(actual)
            self.assertTrue(measured.call_count)
            self.assertTrue(all(row["clip_uid"].startswith("nymeria:") for row in actual))

    def test_catalog_only_reuses_existing_measured_inventory_without_sdk_and_rejects_changed_stats(self):
        self.acquire_fixture()
        (self.root / "_catalog/download_urls.json").unlink()
        actual = nymeria.automatic_candidates(task_name=TASK, min_dur_s=300,
            max_dur_s=1800, root=self.root, include_planned=True)
        self.assertTrue(actual)
        with patch.object(nymeria, "_snapshot", side_effect=AssertionError("catalog must not measure")), \
             patch.object(nymeria, "_sdk_signature", side_effect=AssertionError("catalog must not hash SDK")), \
             patch.object(nymeria_vrs, "_provider", side_effect=AssertionError("catalog must not open SDK")):
            catalog = nymeria.automatic_candidates(task_name=TASK, min_dur_s=300,
                max_dur_s=1800, root=self.root, include_planned=True, catalog_only=True)
            self.assertEqual(catalog, actual)
            (self.seq / "recording_head/data/data.vrs").write_bytes(b"changed source fixture")
            self.assertEqual(nymeria.automatic_candidates(task_name=TASK, min_dur_s=300,
                max_dur_s=1800, root=self.root, include_planned=True, catalog_only=True), [])
