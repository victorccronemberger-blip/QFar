from __future__ import annotations

import unittest
from unittest.mock import patch

from moneymin import ego4d, task_matching as tm


class SensorCoverageSelectionTests(unittest.TestCase):
    def setUp(self):
        self.rule = tm.TaskRule(primary=("Gardening",), evidence=(("garden",),))
        self.rows = [(float(t), "garden", "garden", True, False)
                     for t in range(0, 901, 10)]

    def spans(self, intervals, minimum=60, rows=None):
        return tm._activity_spans(
            self.rule, self.rows if rows is None else rows,
            min_s=minimum, max_s=780, max_gap_s=15,
            video_duration_s=900, allowed_intervals=intervals)

    def test_splits_evidence_before_sensor_gap_instead_of_discarding_whole_run(self):
        spans = self.spans([(0, 300), (310, 620), (630, 900)])
        self.assertEqual([(s["start"], s["end"]) for s in spans],
                         [(0, 300), (310, 620), (630, 900)])

    def test_partial_sensor_capture_preserves_valid_start(self):
        spans = self.spans([(0, 530)], minimum=180)
        self.assertEqual([(s["start"], s["end"]) for s in spans], [(0, 530)])

    def test_no_intervals_is_not_equivalent_to_unrestricted_coverage(self):
        self.assertEqual(self.spans([]), [])
        self.assertEqual(tm.scenario_activity_spans(
            self.rows, min_s=600, max_s=1800, video_duration_s=900,
            allowed_intervals=[]), [])

    def test_long_context_cannot_extend_outside_covered_interval(self):
        rows = [(float(t), "garden", "garden", True, False)
                for t in range(600, 661, 10)]
        spans = self.spans([(300, 900)], minimum=600, rows=rows)
        self.assertTrue(spans)
        self.assertTrue(all(s["start"] >= 300 and s["end"] <= 900 for s in spans))

    def test_intersection_still_rejects_wrong_action_and_short_fragments(self):
        rows = [(t, text, normed, on, t == 150) for t, text, normed, on, _ in self.rows]
        self.assertEqual(self.spans([(0, 300)], minimum=180, rows=rows), [])

    def test_overlapping_components_do_not_duplicate_footage(self):
        spans = self.spans([(0, 300), (200, 500)])
        self.assertEqual([(s["start"], s["end"]) for s in spans], [(0, 500)])


class ScenarioSelectionTests(unittest.TestCase):
    def test_exact_shopping_scene_keeps_five_minute_sensor_windows(self):
        self.video["scenarios"] = ["Grocery shopping indoors"]
        self.video["duration_sec"] = 531
        self.video["imu_metadata"]["component_metadata"] = [{
            "canonical_video_start_ms": 0, "canonical_video_end_ms": 531000}]
        clips = self.results("Shopping", events=[
            (0, "#C C holds the phone"),
            (200, "#C C picks groceries from the shelf"),
            (400, "#C C puts groceries in the cart")], minimum=300)
        self.assertTrue(clips)
        for clip in clips:
            self.assertGreaterEqual(clip["window_s"][0], 60)
            self.assertLessEqual(clip["window_s"][1], 531)
            self.assertGreaterEqual(clip["dur_s"], 300)

    def setUp(self):
        self.video = {
            "video_uid": "parent", "s3_path": "s3://fixture/parent.mp4",
            "scenarios": ["Assembling furniture"], "duration_sec": 1200,
            "has_imu": True, "imu_metadata": {
                "s3_path": "s3://fixture/imu.csv",
                "component_metadata": [{"canonical_video_start_ms": 0,
                                        "canonical_video_end_ms": 1200000}]} }
        self.catalog = ego4d._Catalog({}, {"parent": self.video}, (), {}, {})

    def results(self, name="Furniture Assembly", events=None, minimum=60):
        events = events if events is not None else [(t, "#C C attaches a table part")
                                                    for t in range(0, 1201, 100)]
        with patch.object(ego4d, "_cat", return_value=self.catalog), \
             patch.object(ego4d, "load_timed_narrations", return_value={"parent": tuple(events)}):
            single = ego4d.list_task_spans(name, min_dur_s=minimum, max_dur_s=780)
            ranked = ego4d.rank_all_task_spans(min_dur_s=minimum, max_dur_s=780)[name]
        self.assertEqual({r["clip_uid"] for r in single}, {r["clip_uid"] for r in ranked})
        return single

    def test_lower_minimum_does_not_remove_eligible_long_scenarios(self):
        events = [(100, "#C C looks around")]
        long = self.results(events=events, minimum=600)
        broad = self.results(events=events, minimum=60)
        self.assertTrue(long)
        # A lower floor may divide a safe corridor into shorter windows;
        # it must retain the same covered footage, not necessarily the IDs.
        self.assertEqual(sum(c["dur_s"] for c in long),
                         sum(c["dur_s"] for c in broad))
        self.assertEqual(min(c["window_s"][0] for c in long),
                         min(c["window_s"][0] for c in broad))
        self.assertEqual(max(c["window_s"][1] for c in long),
                         max(c["window_s"][1] for c in broad))

    def test_assembly_evidence_is_not_a_competing_disassembly_boundary(self):
        self.assertNotIn("Furniture Assembly/ Disassembly", tm.competing_span_names(
            "Furniture Assembly", tm.TASK_RULES.items()))
        clips = self.results(minimum=600)
        self.assertTrue(clips)
        self.assertTrue(any(c["action_units"] for c in clips))

    def test_scenario_still_excludes_contradictory_actions(self):
        self.video["scenarios"] = ["Walking the dog / pet"]
        clips = self.results("Walk the Dog", events=[
            (0, "#C C walks a dog"), (600, "#C C pushes a baby stroller"),
            (1190, "#C C walks a dog")], minimum=600)
        self.assertEqual(clips, [])


if __name__ == "__main__":
    unittest.main()
