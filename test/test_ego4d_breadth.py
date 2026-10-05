from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from moneymin import campaign, ego4d, task_matching


class NarratedBreadthTests(unittest.TestCase):
    def setUp(self):
        self.video = {"video_uid": "parent", "duration_sec": 700,
                      "scenarios": ["Cooking"], "has_imu": True,
                      "s3_path": "s3://fixture/parent.mp4",
                      "imu_metadata": {"s3_path": "s3://fixture/imu.csv",
                          "component_metadata": [{"canonical_video_start_ms": 0,
                                                  "canonical_video_end_ms": 700000}]}}
        self.rule = task_matching.rule_for("Folding Clothes or Putting Them on Hangers")
        self.events = [(float(t), "#C C folds the shirt") for t in range(0, 601, 5)]

    def select(self, events=None, rule=None, name="Folding Clothes or Putting Them on Hangers"):
        catalog = ego4d._Catalog({}, {"parent": self.video}, (), {}, {})
        with patch.object(ego4d, "_cat", return_value=catalog):
            return ego4d._narration_evidence_for_video(
                self.video, self.events if events is None else events,
                [(name, self.rule if rule is None else rule)],
                min_dur_s=60, max_dur_s=1800)

    def test_specific_timed_action_survives_broad_parent_label(self):
        found = self.select()
        self.assertTrue(found)
        self.assertTrue(all(clip["dur_s"] >= 60 for _, clip in found))
        self.assertTrue(all(clip["window_s"][1] <= 600 for _, clip in found))

    def test_single_and_bulk_activity_selection_include_the_same_new_source(self):
        name = "Folding Clothes or Putting Them on Hangers"
        catalog = ego4d._Catalog({}, {"parent": self.video}, (), {}, {})
        with patch.object(ego4d, "_cat", return_value=catalog), patch.object(
                ego4d, "load_timed_narrations", return_value={"parent": tuple(self.events)}):
            single = ego4d.list_task_spans(name)
            bulk = ego4d.rank_all_task_spans()[name]
        self.assertTrue(single)
        self.assertEqual({c["clip_uid"] for c in single}, {c["clip_uid"] for c in bulk})

    def test_single_mention_cannot_approve_whole_video(self):
        self.assertEqual(self.select([(10, "#C C folds the shirt")]), [])

    def test_other_person_is_not_the_camera_wearer(self):
        self.assertEqual(self.select([(t, "#O O folds the shirt") for t, _ in self.events]), [])

    def test_explicit_exclusion_remains_effective(self):
        self.assertEqual(self.select(rule=replace(self.rule, excluded=("Cooking",))), [])

    def test_location_dependent_task_does_not_cross_scenarios(self):
        self.video["scenarios"] = ["Gardening"]
        rule = task_matching.rule_for("Water Houseplants")
        self.assertEqual(self.select([(t, "#C C waters the plants") for t, _ in self.events],
                                     rule=rule, name="Water Houseplants"), [])

    def test_phone_event_splits_the_selected_action(self):
        found = self.select(self.events + [(300, "#C C uses the phone")])
        self.assertTrue(found)
        self.assertTrue(all(not (clip["window_s"][0] < 300 < clip["window_s"][1])
                            for _, clip in found))

    def test_missing_sensor_data_remains_unavailable(self):
        self.video["has_imu"] = False
        catalog = ego4d._Catalog({}, {"parent": self.video}, (), {}, {})
        with patch.object(ego4d, "_cat", return_value=catalog), patch.object(
                ego4d, "load_timed_narrations", return_value={"parent": tuple(self.events)}):
            self.assertFalse(any(ego4d.narration_evidence_clips().values()))


class CandidateCoverageTests(unittest.TestCase):
    def clip(self, uid, start, end):
        return {"clip_uid": uid, "parent_video_uid": "parent",
                "window_s": (start, end), "dur_s": end - start}

    def test_multiple_short_windows_can_cover_a_candidate(self):
        result = campaign._union_ranked_clips(
            {"Gardening": [self.clip("a", 0, 250), self.clip("b", 250, 500)]},
            {"Gardening": [self.clip("extra", 0, 600)]})
        self.assertEqual([c["clip_uid"] for c in result["Gardening"]], ["a", "b"])

    def test_overlapping_short_windows_are_not_counted_twice(self):
        result = campaign._union_ranked_clips(
            {"Gardening": [self.clip("a", 0, 250), self.clip("b", 0, 250)]},
            {"Gardening": [self.clip("extra", 0, 600)]})
        self.assertEqual([c["clip_uid"] for c in result["Gardening"]], ["a", "b", "extra"])
        self.assertEqual(set(result["Gardening"][-1]["dedup_clip_uids"]), {"a", "b"})

    def test_longer_candidate_respects_short_clip_sent_history(self):
        result = campaign._union_ranked_clips(
            {"Gardening": [self.clip("old", 0, 100)]},
            {"Gardening": [self.clip("long", 0, 600)]})
        with patch.object(campaign.sent_registry, "sent_emails",
                          side_effect=lambda key, uid: {"sent@example.com"} if uid == "old" else set()):
            self.assertEqual(campaign._candidate_sent_emails("source", result["Gardening"][-1]),
                             {"sent@example.com"})


class WindowRevalidationTests(unittest.TestCase):
    def select(self, text, scenarios=("Cooking",), minimum=0):
        name = "Folding Clothes or Putting Them on Hangers"
        video = {"video_uid": "p", "duration_sec": 600, "has_imu": True,
                 "scenarios": list(scenarios), "s3_path": "s3://fixture/p.mp4",
                 "imu_metadata": {"s3_path": "s3://fixture/p.csv", "component_metadata": [
                     {"canonical_video_start_ms": minimum * 1000,
                      "canonical_video_end_ms": 600000}]}}
        events = tuple((float(t), text) for t in range(0, 601, 5))
        catalog = ego4d._Catalog({}, {"p": video}, (), {}, {})
        candidates = {name: [{"clip_uid": "legacy", "parent_video_uid": "p",
                              "window_s": (0, 600), "match_score": 9999}]}
        with patch.object(ego4d, "_cat", return_value=catalog), patch.object(
                ego4d, "load_timed_narrations", return_value={"p": events}):
            return ego4d.revalidate_task_windows(candidates).get(name, [])

    def test_valid_old_duration_window_is_proven_again(self):
        found = self.select("#C C folds the shirt")
        self.assertTrue(found)
        self.assertTrue(all(c["window_s"][0] >= 0 and c["window_s"][1] <= 600 for c in found))

    def test_old_score_cannot_approve_wrong_activity(self):
        self.assertEqual(self.select("#C C cooks food"), [])

    def test_other_actor_cannot_approve_recovered_window(self):
        self.assertEqual(self.select("#O O folds the shirt"), [])

    def test_old_window_with_missing_imu_is_rejected(self):
        self.assertEqual(self.select("#C C folds the shirt", minimum=20), [])


class LocalSensorCatalogTests(unittest.TestCase):
    def test_sensor_grid_edge_does_not_discard_a_valid_clip(self):
        from scripts.build_ego4d_task_seed import validate_local_sensors
        clip = {"clip_uid": "c", "parent_video_uid": "p", "dur_s": 600,
                "window_s": (0, 600)}
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "p_imu.csv").write_text("fixture")
            with patch.object(ego4d, "EGO4D_DIR", Path(root)), patch(
                    "scripts.build_ego4d_task_seed.local_sensor_intervals",
                    return_value=[(0.002, 599.998)]), patch.object(
                    ego4d, "build_imu_csv", return_value="") as validate:
                result = validate_local_sensors({"Gardening": (clip,)})
                self.assertEqual([c["clip_uid"] for c in result["Gardening"]], ["c"])
                validate.assert_called_once()

    def test_actual_gaps_and_missing_second_sensor_split_coverage(self):
        from scripts.build_ego4d_task_seed import local_sensor_intervals
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "imu.csv"
            lines = ["canonical_timestamp_ms,gyro_x,gyro_y,gyro_z,accl_x,accl_y,accl_z"]
            # Spacing 20 ms stays inside the Minute 1.29 25 ms max span;
            # the 500–600 ms gyro hole still splits coverage.
            for t in (0, 20, 40, 60, 80, 100, 200, 220, 240, 260, 280, 300):
                gyro = ",," if t in (200, 220, 240) else "1,2,3"
                lines.append(f"{t},{gyro},4,5,6")
            path.write_text("\n".join(lines))
            self.assertEqual(local_sensor_intervals(path), [(0, 0.1), (0.26, 0.3)])


class CatalogRefreshTests(unittest.TestCase):
    def test_clip_manifest_participates_in_persistent_cache_signature(self):
        with tempfile.TemporaryDirectory() as root, patch.object(campaign.config, "MEDIA_DATA_DIR", Path(root)):
            path = Path(root) / "ego4d" / "clips.csv"
            path.parent.mkdir()
            path.write_text("before")
            campaign._rank_cache_stamp.cache_clear()
            before = campaign._rank_cache_stamp()
            path.write_text("after!")
            campaign._rank_cache_stamp.cache_clear()
            self.assertNotEqual(before, campaign._rank_cache_stamp())
        campaign._rank_cache_stamp.cache_clear()

    def test_expansion_invalidates_in_memory_ranking_without_restart(self):
        with tempfile.TemporaryDirectory() as root, patch.object(campaign.config, "MEDIA_DATA_DIR", Path(root)), \
             patch.object(campaign, "_RANK_INPUT_SIGNATURE", None), \
             patch.object(campaign, "_ranked_pools_cached", return_value={}) as cached:
            path = Path(root) / "ego4d" / "clips.csv"
            path.parent.mkdir()
            path.write_text("before")
            campaign._ranked_pools()
            campaign._ranked_pools()
            self.assertEqual(cached.cache_clear.call_count, 1)
            path.write_text("larger manifest")
            campaign._ranked_pools()
            self.assertEqual(cached.cache_clear.call_count, 2)


class FiveMinuteCoverageTests(unittest.TestCase):
    def setUp(self):
        self.rule = task_matching.TaskRule(primary=("Gardening",), evidence=(("garden",),))

    def select(self, times, minimum=300):
        rows = [(float(t), "garden", "garden", True, False) for t in times]
        return task_matching._activity_spans(
            self.rule, rows, min_s=minimum, max_s=1800, max_gap_s=15,
            video_duration_s=1200, allowed_intervals=[(0, 1200)])

    def test_five_minute_activity_accepts_recurring_sparse_annotations(self):
        spans = self.select(range(0, 321, 40))
        self.assertTrue(spans)
        self.assertEqual((spans[0]["start"], spans[0]["end"]), (0, 320))

    def test_five_minute_activity_does_not_expand_short_core(self):
        self.assertEqual(self.select(range(300, 361, 5)), [])

    def test_one_minute_request_keeps_proofs_three_minutes_apart(self):
        spans = self.select(range(0, 321, 40), minimum=60)
        self.assertTrue(spans)
        self.assertEqual((spans[0]["start"], spans[0]["end"]), (0, 320))

    def test_proofs_more_than_three_minutes_apart_stay_separate(self):
        spans = self.select([0, 40, 80, 280, 320, 360], minimum=60)
        self.assertEqual([(span["start"], span["end"]) for span in spans],
                         [(0, 80), (280, 360)])

    def test_phone_event_prevents_joining_two_short_activities(self):
        rows = [(float(t), "garden", "garden", True, False) for t in range(0, 601, 40)]
        rows.append((280, "phone", "phone", False, True))
        rows.sort()
        result = task_matching._activity_spans(
            self.rule, rows, min_s=300, max_s=1800, max_gap_s=15,
            video_duration_s=1200, allowed_intervals=[(0, 1200)])
        self.assertTrue(all(not (s["start"] < 280 < s["end"]) for s in result))

    def test_washing_bicycle_inside_bathroom_is_not_cleaning_bathroom(self):
        rule = task_matching.rule_for("Clean the Bathroom")
        prepared = task_matching.prepare_span_events(
            [(float(t), "#C C cleans bicycle in the toilet") for t in range(0, 601, 10)])
        labels = task_matching.label_span_events(prepared, [("Clean the Bathroom", rule)])
        self.assertFalse(any(labels))


class CatalogHistoryTests(unittest.TestCase):
    def row(self, uid, window):
        return {"clip_uid": uid, "parent_video_uid": "parent", "window_s": window}

    def test_overlapping_old_identifiers_still_block_resending(self):
        old = self.row("old", (0, 400))
        old["dedup_clip_uids"] = ["legacy"]
        fresh = self.row("new", (10, 410))
        linked = campaign._link_rank_history({"Cleaning Car": (fresh,)}, {"Cleaning Car": (old,)})
        self.assertEqual(linked["Cleaning Car"][0]["dedup_clip_uids"], ["legacy", "old"])
        self.assertNotIn("dedup_clip_uids", fresh)

    def test_disjoint_unused_content_remains_new(self):
        fresh = self.row("new", (400, 800))
        linked = campaign._link_rank_history({"Cleaning Car": (fresh,)},
                                            {"Cleaning Car": (self.row("old", (0, 400)),)})
        self.assertNotIn("dedup_clip_uids", linked["Cleaning Car"][0])

    def test_rejected_old_content_is_not_restored(self):
        linked = campaign._link_rank_history({"Cleaning Car": ()},
                                            {"Cleaning Car": (self.row("old", (0, 400)),)})
        self.assertEqual(linked["Cleaning Car"], ())

    def test_canonical_task_aliases_share_history(self):
        linked = campaign._link_rank_history({"Clean the Bathroom": (self.row("new", (0, 400)),)},
                                            {"Bathroom Deep Clean": (self.row("old", (0, 400)),)})
        self.assertEqual(linked["Clean the Bathroom"][0]["dedup_clip_uids"], ["old"])
