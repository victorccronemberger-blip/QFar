"""Current task eligibility and duration segmentation, with inert local inputs."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from moneymin import campaign, ego4d, task_matching
from moneymin.campaign_types import CampaignConfig, TaskSpec


class CampaignSelectionEvidenceRegressions(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.media = self.root / "ego4d"
        self.media.mkdir()
        self.meta = self.media / "ego4d.json"
        self.clips = self.media / "clips.csv"
        self.timed = self.media / "timed_narrations.jsonl"
        self.video = {
            "video_uid": "parent", "duration_sec": 600, "has_imu": True,
            "s3_path": "s3://fixture/parent.mp4", "scenarios": ["Gardening"],
            "imu_metadata": {
                "s3_path": "s3://fixture/imu.csv",
                "component_metadata": [{"canonical_video_start_ms": 0,
                                        "canonical_video_end_ms": 600000}],
            },
        }
        self.clips.write_text("exported_clip_uid,parent_video_uid,parent_start_sec,"
                              "parent_end_sec,s3_path\n", encoding="utf-8")
        for obj, key, value in ((campaign.config, "DATA_DIR", self.root),
                                (campaign.config, "MEDIA_DATA_DIR", self.root),
                                (ego4d, "EGO4D_DIR", self.media),
                                (campaign, "_RANK_INPUT_SIGNATURE", None)):
            self.stack.enter_context(patch.object(obj, key, value))
        self.stack.enter_context(patch.object(ego4d, "sync_meta",
                                             return_value=(self.meta, self.clips)))
        self.stack.enter_context(patch.object(ego4d, "_download_to",
                                             side_effect=AssertionError("No network")))
        self.stack.enter_context(patch.object(campaign, "_load_rank_seed", return_value={}))
        for cached in (campaign._task_candidates, campaign._ranked_pools_cached,
                       campaign._duration_ranked_pools):
            cached.cache_clear()
            self.addCleanup(cached.cache_clear)

    def write_events(self, events, scenario="Gardening"):
        self.video["scenarios"] = [scenario]
        self.meta.write_text(json.dumps({"videos": [self.video]}), encoding="utf-8")
        self.timed.write_text(json.dumps({"video_uid": "parent", "events": events}) + "\n",
                              encoding="utf-8")

    def test_other_scene_activity_blocks_ranking_and_fresh_revalidation(self):
        events = [(t, "#C C waters the garden") for t in range(0, 61, 10)]
        events += [(t, "#C C assembles table with pieces and bolts")
                   for t in range(100, 601, 10)]
        self.write_events(events)
        single = ego4d.list_task_spans("Gardening")
        bulk = ego4d.rank_all_task_spans()["Gardening"]
        self.assertEqual({clip["clip_uid"] for clip in single},
                         {clip["clip_uid"] for clip in bulk})
        self.assertTrue(bulk)
        self.assertTrue(all(clip["window_s"][1] <= 100 for clip in bulk))
        foreign = ego4d._span_record(self.video, {"start": 300, "end": 600})
        foreign = ego4d.attach_selection_evidence(foreign, "Gardening")
        self.assertEqual(ego4d.revalidate_task_windows({"Gardening": [foreign]}), {})
        with self.assertRaisesRegex(ValueError, "current task evidence"):
            ego4d.revalidate_selection_evidence(foreign, task_name="Gardening")
        self.assertFalse(campaign._prepare_queue_accepts(foreign, "Gardening"))

    def test_real_clothesline_window_rejects_fresh_hanger_and_folding_carriers(self):
        # Human annotations for 66f93990 contain two mentions of a hanger
        # among 102 events dominated by a clothesline/rack. Neither task
        # permits this three-minute drying sequence as proof of hangers.
        real = json.loads((Path(__file__).parent / "fixtures" /
                           "ego4d_clothesline_window.json").read_text(encoding="utf-8"))
        self.write_events(real["events"], scenario="Cleaning / laundry")
        window = real["window_s"]
        candidate = ego4d._span_record(self.video, {"start": window[0], "end": window[1]})
        for name in ("Hanging clothes on hangers",
                     "Folding Clothes or Putting Them on Hangers"):
            with self.subTest(task=name):
                rebound = ego4d.attach_selection_evidence(
                    candidate, name, task_id="ae3d724f-f1bb-4576-af53-ae2c94de0189",
                    registry_key="minute|ae3d724f-f1bb-4576-af53-ae2c94de0189|fixture")
                self.assertEqual(ego4d.revalidate_task_windows(
                    {name: [rebound]}, min_dur_s=180, max_dur_s=780), {})
                with self.assertRaisesRegex(ValueError, "current task evidence"):
                    ego4d.revalidate_selection_evidence(rebound, task_name=name,
                        task_id="ae3d724f-f1bb-4576-af53-ae2c94de0189",
                        registry_key="minute|ae3d724f-f1bb-4576-af53-ae2c94de0189|fixture")

    def test_explicit_hangers_and_folding_remain_current_evidence(self):
        for action, tasks in (
                ("#C C places the shirt on the hanger",
                 ("Hanging clothes on hangers", "Folding Clothes or Putting Them on Hangers")),
                ("#C C folds the shirt", ("Folding Clothes or Putting Them on Hangers",)),
                ("#C C folds the towel", ("Folding Clothes or Putting Them on Hangers",)),
                ("#C C folds the clothes", ("Folding Clothes or Putting Them on Hangers",))):
            self.write_events([(t, action) for t in range(0, 601, 10)],
                              scenario="Cleaning / laundry")
            candidate = ego4d._span_record(self.video, {"start": 0, "end": 600})
            for name in tasks:
                with self.subTest(action=action, task=name):
                    rebound = ego4d.attach_selection_evidence(candidate, name)
                    self.assertEqual(ego4d.revalidate_selection_evidence(rebound, task_name=name)
                                     ["justification"]["status"], "locally_revalidated")
            if "folds" in action:
                folded = ego4d.attach_selection_evidence(candidate, "Hanging clothes on hangers")
                with self.assertRaisesRegex(ValueError, "current task evidence"):
                    ego4d.revalidate_selection_evidence(folded, task_name="Hanging clothes on hangers")

    def test_generic_hanging_and_storage_do_not_prove_hangers_or_folding(self):
        for action in ("#C C hangs the clothes on the clothesline",
                       "#C C places the shirt on the clothes rack",
                       "#C C hangs the shirt", "#C C looks at clothes in the closet"):
            for name in ("Hanging clothes on hangers",
                         "Folding Clothes or Putting Them on Hangers"):
                with self.subTest(action=action, task=name):
                    self.assertIsNone(task_matching.score_action(task_matching.rule_for(name), action))

    def test_real_sewing_windows_reject_fresh_laundry_carriers(self):
        real = json.loads((Path(__file__).parent / "fixtures" /
                           "ego4d_sewing_windows.json").read_text(encoding="utf-8"))
        self.video["duration_sec"] = 4000
        self.video["imu_metadata"]["component_metadata"][0]["canonical_video_end_ms"] = 4000000
        for uid, sewing in real.items():
            self.write_events(sewing["events"], scenario=sewing["scenarios"][0])
            start, end = sewing["window_s"]
            self.assertTrue(ego4d.imu_window_is_covered(self.video, (start, end)))
            candidate = ego4d._span_record(self.video, {"start": start, "end": end})
            for name in ("Hanging clothes on hangers",
                         "Folding Clothes or Putting Them on Hangers"):
                with self.subTest(parent=uid, task=name):
                    self.assertIsNotNone(task_matching.score_narrated_scenarios(
                        name, task_matching.rule_for(name), sewing["scenarios"]))
                    rebound = ego4d.attach_selection_evidence(candidate, name,
                        task_id="ae3d724f-f1bb-4576-af53-ae2c94de0189",
                        registry_key="minute|ae3d724f-f1bb-4576-af53-ae2c94de0189|fixture")
                    self.assertEqual(ego4d.revalidate_task_windows(
                        {name: [rebound]}, min_dur_s=180, max_dur_s=780), {})
                    with self.assertRaisesRegex(ValueError, "current task evidence"):
                        ego4d.revalidate_selection_evidence(rebound, task_name=name,
                            task_id="ae3d724f-f1bb-4576-af53-ae2c94de0189",
                            registry_key="minute|ae3d724f-f1bb-4576-af53-ae2c94de0189|fixture")

    def test_generic_fabric_and_sewing_context_do_not_prove_folding_laundry(self):
        rule = task_matching.rule_for("Folding Clothes or Putting Them on Hangers")
        for action in ("#C C folds the piece of cloth", "#C C folds the fabric",
                       "#C C folds the trouser lining #C C puts the trouser on the sewing machine",
                       "#C C folds the shirt #C C sews the shirt"):
            with self.subTest(action=action):
                self.assertIsNone(task_matching.score_action(rule, action))

    def test_duration_recuts_are_verified_cached_and_linked_to_history(self):
        task = "Folding Clothes or Putting Them on Hangers"
        self.write_events([(t, "#C C folds the shirt") for t in range(0, 601, 10)],
                          scenario="Cooking")
        with patch.object(ego4d, "revalidate_task_windows",
                          wraps=ego4d.revalidate_task_windows) as resegment:
            first = campaign._compatible_task_clips(task, "ego4d", min_dur_s=120,
                                                     max_dur_s=240)
            second = campaign._compatible_task_clips(task, "ego4d", min_dur_s=120,
                                                      max_dur_s=240)
            self.assertEqual(resegment.call_count, 1)
            self.assertEqual(first, second)
            # A disk hit must also avoid reparsing all source activities.
            campaign._duration_ranked_pools.cache_clear()
            third = campaign._compatible_task_clips(task, "ego4d", min_dur_s=120,
                                                     max_dur_s=240)
            self.assertEqual(resegment.call_count, 1)
        self.assertEqual({tuple(clip["window_s"]) for clip in third},
                         {(0., 240.), (250., 490.)})
        for clip in third:
            self.assertIn("parent_0.000_600.000", clip["dedup_clip_uids"])
            self.assertEqual(ego4d.revalidate_selection_evidence(clip, task_name=task)
                             ["justification"]["status"], "locally_revalidated")

    def test_higher_minimum_rechecks_adjacent_indexed_scene_windows(self):
        self.video["duration_sec"] = 1200
        self.video["imu_metadata"]["component_metadata"][0]["canonical_video_end_ms"] = 1200000
        self.write_events([(100, "#C C looks around")], scenario="Assembling furniture")
        default = campaign._compatible_task_clips("Furniture Assembly", "ego4d")
        self.assertTrue(default)
        self.assertTrue(all(clip["dur_s"] == 300 for clip in default))
        longer = campaign._compatible_task_clips("Furniture Assembly", "ego4d",
                                                 min_dur_s=600, max_dur_s=1200)
        self.assertEqual({tuple(clip["window_s"]) for clip in longer},
                         {(0., 600.), (600., 1200.)})

    def test_category_counts_use_queue_evidence_in_one_source_snapshot(self):
        task = "Folding Clothes or Putting Them on Hangers"
        self.write_events([(t, "#C C cooks food") for t in range(0, 601, 10)],
                          scenario="Cleaning / laundry")
        old = ego4d._span_record(self.video, {"start": 0, "end": 600})
        self.stack.enter_context(patch.object(campaign, "_load_rank_seed",
                                              return_value={task: (old,)}))
        reads = []
        source = ego4d._selection_source

        def observed(path):
            snapshot = ego4d._SELECTION_SNAPSHOT.get()
            if snapshot is None or str(Path(path).absolute()) not in snapshot:
                reads.append(Path(path))
            return source(path)

        with patch.object(ego4d, "_selection_source", side_effect=observed):
            rows = campaign.available_tasks(
                "inert@example.invalid", "inert-org", dataset_provider="ego4d",
                content_mode="dataset", include_unavailable=True,
                remote_tasks=[{"id": "task", "name": task}])
        self.assertEqual(reads.count(self.timed), 1)
        self.assertEqual(rows[0]["clip_count"], 0)
        self.assertEqual(rows[0]["overall_clip_count"], 0)
        self.assertFalse(rows[0]["available_for_duration"])
        specification = TaskSpec("task", "Cleaning / laundry", 60, 1800, task_name=task)
        configuration = CampaignConfig(accounts=[], tasks=[specification], work_dir=self.media,
                                       dataset_provider="ego4d", content_mode="dataset")
        self.assertEqual(campaign.automatic_candidates(specification, configuration), [])

    def test_effect_revalidation_keeps_fresh_default_inside_catalog_snapshot(self):
        self.write_events([(t, "#C C folds the shirt") for t in range(0, 601, 10)],
                          scenario="Cooking")
        task = "Folding Clothes or Putting Them on Hangers"
        with ego4d.selection_operation():
            clip = ego4d.rank_all_task_spans()[task][0]
            self.write_events([(t, "#C C cooks food") for t in range(0, 601, 10)],
                              scenario="Cooking")
            with self.assertRaisesRegex(ValueError, "selection changed"):
                ego4d.revalidate_selection_evidence(clip, task_name=task)

    def test_empty_duration_sources_never_load_or_sync_catalog(self):
        with patch.object(ego4d, "_cat", side_effect=AssertionError("Empty sources")), \
             patch.object(ego4d, "load_timed_narrations", side_effect=AssertionError("Empty sources")):
            self.assertEqual(ego4d.revalidate_task_windows({}), {})
            self.assertEqual(ego4d.revalidate_task_windows({"Gardening": []}), {})
        with patch.object(campaign, "_load_rank_cache", return_value=None), \
             patch.object(campaign, "_ranked_pools_cached", return_value={
                 "Gardening": ({"clip_uid": "portable", "dur_s": 200},)}), \
             patch.object(campaign, "_save_rank_cache"), \
             patch.object(ego4d, "has_timed_narrations", return_value=True), \
             patch.object(ego4d, "revalidate_task_windows",
                          side_effect=AssertionError("No timed source windows")):
            result = campaign._duration_ranked_snapshot(("empty-source-audit",), 180, 780)
        self.assertEqual(result["Gardening"][0]["clip_uid"], "portable")

    def test_automatic_queue_shares_inputs_between_candidates_but_effects_refresh(self):
        task = "Folding Clothes or Putting Them on Hangers"
        self.write_events([(t, "#C C folds the shirt") for t in range(0, 601, 10)],
                          scenario="Cooking")
        clip = ego4d.rank_all_task_spans()[task][0]
        specification = TaskSpec("task", "Cleaning / laundry", 180, 780, task_name=task)
        configuration = CampaignConfig(accounts=[], tasks=[specification], work_dir=self.media,
                                       dataset_provider="ego4d", content_mode="dataset")
        reads = []
        source = ego4d._selection_source

        def observed(path):
            snapshot = ego4d._SELECTION_SNAPSHOT.get()
            if snapshot is None or str(Path(path).absolute()) not in snapshot:
                reads.append(Path(path))
            return source(path)

        with patch.object(campaign, "_compatible_task_clips", return_value=[clip, clip]), \
             patch.object(ego4d, "_selection_source", side_effect=observed):
            actual = campaign.automatic_candidates(specification, configuration)
        self.assertEqual(len(actual), 2)
        self.assertEqual(reads.count(self.timed), 1)
        self.assertEqual(reads.count(self.meta), 1)
        self.write_events([(t, "#C C cooks food") for t in range(0, 601, 10)],
                          scenario="Cooking")
        with self.assertRaisesRegex(ValueError, "selection changed"):
            ego4d.revalidate_selection_evidence(actual[0], task_name=task)


if __name__ == "__main__":
    unittest.main()
