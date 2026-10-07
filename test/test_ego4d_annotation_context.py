"""Reusing parent annotations must not retain approval across effect boundaries."""
from contextlib import ExitStack
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from moneymin import ego4d, task_matching


class AnnotationContextTests(unittest.TestCase):
    task = "Folding Clothes or Putting Them on Hangers"

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.root.joinpath("ego4d.json").write_text(json.dumps({"videos": [{
            "video_uid": "parent", "duration_sec": 600, "has_imu": True,
            "s3_path": "s3://fixture/parent.mp4", "scenarios": ["Cleaning / laundry"],
            "imu_metadata": {"s3_path": "s3://fixture/parent_imu.csv", "component_metadata": [
                {"canonical_video_start_ms": 0, "canonical_video_end_ms": 600000}]},
        }]}), encoding="utf-8")
        self.root.joinpath("clips.csv").write_text(
            "exported_clip_uid,parent_video_uid,parent_start_sec,parent_end_sec,s3_path\n",
            encoding="utf-8")
        self.timed = self.root / "timed_narrations.jsonl"
        self.write_events("#C C folds the shirt")
        self.stack.enter_context(patch.object(ego4d, "EGO4D_DIR", self.root))
        self.stack.enter_context(patch.object(ego4d, "timed_narrations_path", return_value=self.timed))
        self.stack.enter_context(patch.object(ego4d, "sync_meta", return_value=(
            self.root / "ego4d.json", self.root / "clips.csv")))
        self.stack.enter_context(patch.object(ego4d, "_download_to", side_effect=AssertionError("No network")))
        self.candidate = {self.task: [{"clip_uid": "parent_0.000_600.000",
                                     "parent_video_uid": "parent", "window_s": (0, 600)}]}

    def write_events(self, action):
        self.timed.write_text(json.dumps({"video_uid": "parent", "events": [
            [second, action] for second in range(0, 601, 5)]}) + "\n", encoding="utf-8")

    def recheck(self):
        return ego4d.revalidate_task_windows(self.candidate, min_dur_s=300, max_dur_s=600)

    def test_multiple_windows_classify_parent_once_per_operation(self):
        with patch.object(task_matching, "prepare_span_events", wraps=task_matching.prepare_span_events) as prepare, \
             patch.object(task_matching, "label_span_events", wraps=task_matching.label_span_events) as classify:
            with ego4d.selection_operation():
                first = self.recheck()
                second = self.recheck()
                self.assertTrue(first[self.task])
                self.assertEqual(first, second)
                self.assertEqual(prepare.call_count, 1)
                self.assertEqual(classify.call_count, 1)
            self.assertEqual(self.recheck(), first)
            self.assertEqual(prepare.call_count, 2)
            self.assertEqual(classify.call_count, 2)

    def test_fresh_boundary_rechecks_changed_annotation_inside_old_operation(self):
        with ego4d.selection_operation():
            self.assertTrue(self.recheck()[self.task])
            self.write_events("#C C cooks food")
            # The read-only operation continues to use its bound source bytes.
            self.assertTrue(self.recheck()[self.task])
            with ego4d.selection_operation(fresh=True):
                self.assertEqual(self.recheck().get(self.task, []), [])

    def test_bulk_ranking_and_effect_check_share_annotation_classification(self):
        with patch.object(task_matching, "prepare_span_events", wraps=task_matching.prepare_span_events) as prepare, \
             patch.object(task_matching, "label_span_events", wraps=task_matching.label_span_events) as classify:
            with ego4d.selection_operation():
                self.assertTrue(ego4d.rank_all_task_spans().get(self.task))
                self.assertTrue(ego4d.narration_evidence_clips().get(self.task))
                self.assertTrue(self.recheck().get(self.task))
                self.assertEqual(prepare.call_count, 1)
                self.assertEqual(classify.call_count, 1)

    def test_mutable_annotation_input_is_not_memoized(self):
        events = [(float(t), "#C C folds the shirt") for t in range(0, 601, 5)]
        with ego4d.selection_operation(), patch.object(
                task_matching, "prepare_span_events", wraps=task_matching.prepare_span_events) as prepare:
            original = ego4d._task_annotation_context("parent", events)
            events[:] = [(t, "#C C cooks food") for t, _ in events]
            changed = ego4d._task_annotation_context("parent", events)
            self.assertNotEqual(original[2], changed[2])
            self.assertEqual(prepare.call_count, 2)

    def test_rule_change_cannot_reuse_previous_classification(self):
        with ego4d.selection_operation():
            self.assertTrue(self.recheck()[self.task])
            changed = replace(task_matching.TASK_RULES[self.task], action_excluded=("fold",))
            with patch.dict(task_matching.TASK_RULES, {self.task: changed}):
                self.assertEqual(self.recheck().get(self.task, []), [])


if __name__ == "__main__":
    unittest.main()
