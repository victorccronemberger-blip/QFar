from __future__ import annotations

import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from moneymin import ego4d, task_matching
from moneymin.ego4d import (
    _Catalog,
    _export_span_validator,
    _iter_span_records,
    _uses_exported_clip,
    prefer_long_clips,
)
from moneymin.task_matching import (
    TaskRule,
    _activity_spans,
    prepare_span_events,
    scenario_activity_spans,
)


class LongTaskSpanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rule = TaskRule(primary=("Gardening",), evidence=(("garden",),))

    @staticmethod
    def rows(times: list[float]):
        return [(time, "garden", "garden", True, False) for time in times]

    def test_long_mode_accepts_sparse_but_recurring_evidence(self) -> None:
        spans = _activity_spans(
            self.rule,
            self.rows([0, 100, 200, 300, 400, 500, 600, 700]),
            min_s=600,
            max_s=1800,
            max_gap_s=15,
            video_duration_s=900,
        )

        self.assertEqual(len(spans), 1)
        self.assertEqual((spans[0]["start"], spans[0]["end"]), (0, 700))

    def test_long_mode_expands_a_verified_core_through_safe_context(self) -> None:
        spans = _activity_spans(
            self.rule,
            self.rows([300, 310, 320, 330, 340, 350, 360]),
            min_s=600,
            max_s=1800,
            max_gap_s=15,
            video_duration_s=900,
        )

        expanded = [
            span for span in spans
            if span.get("expanded_from_verified_core")
        ]
        self.assertEqual(len(expanded), 1)
        self.assertGreaterEqual(expanded[0]["end"] - expanded[0]["start"], 600)

    def test_long_expansion_does_not_cross_a_boundary(self) -> None:
        rows = [(250, "unsafe", "unsafe", False, True)]
        rows.extend(self.rows([300, 320, 340, 360]))
        rows.append((500, "other task", "other task", False, True))

        spans = _activity_spans(
            self.rule,
            rows,
            min_s=600,
            max_s=1800,
            max_gap_s=15,
            video_duration_s=900,
        )

        self.assertEqual(spans, [])

    def test_long_expansion_moves_inside_sensor_coverage(self) -> None:
        spans = _activity_spans(
            self.rule,
            self.rows([600, 610, 620, 630, 640, 650, 660]),
            min_s=600,
            max_s=1800,
            max_gap_s=15,
            video_duration_s=1200,
            allowed_intervals=[(300, 1000)],
        )

        expanded = [
            span for span in spans
            if span.get("expanded_from_verified_core")
        ]
        self.assertEqual(len(expanded), 1)
        self.assertGreaterEqual(expanded[0]["start"], 300)
        self.assertLessEqual(expanded[0]["end"], 1000)

    def test_competing_or_unsafe_event_still_splits_long_span(self) -> None:
        rows = self.rows([0, 100, 200, 300])
        rows.append((350, "other task", "other task", False, True))
        rows.extend(self.rows([450, 550, 650, 750, 850, 900]))

        spans = _activity_spans(
            self.rule,
            rows,
            min_s=600,
            max_s=1800,
            max_gap_s=15,
            video_duration_s=1000,
        )

        self.assertEqual(spans, [])

    def test_short_mode_keeps_strict_narration_gaps(self) -> None:
        spans = _activity_spans(
            self.rule,
            self.rows([0, 20, 40, 60, 80]),
            min_s=60,
            max_s=300,
            max_gap_s=15,
            video_duration_s=120,
        )

        self.assertEqual(spans, [])

    def test_unsure_object_is_neutral_not_a_safety_boundary(self) -> None:
        prepared = prepare_span_events([
            (10, "#C C cleans the garden"),
            (20, "#C C picks #unsure"),
        ])

        self.assertEqual(prepared[1][2], "")
        self.assertFalse(prepared[1][3])

    def test_real_hygiene_warning_remains_a_boundary(self) -> None:
        prepared = prepare_span_events([
            (10, "#C C cleans the garden"),
            (20, "#C C picks up the phone"),
        ])

        self.assertTrue(prepared[1][3])

    def test_exact_scenario_respects_a_narrow_duration_range(self) -> None:
        spans = scenario_activity_spans(
            self.rows([100, 700, 1300]),
            min_s=600,
            max_s=720,
            video_duration_s=1799,
        )

        self.assertEqual(len(spans), 2)
        self.assertTrue(all(600 <= span["end"] - span["start"] <= 720
                            for span in spans))

    def test_exact_scenario_is_split_into_long_safe_windows(self) -> None:
        spans = scenario_activity_spans(
            self.rows([100, 300, 700, 1000, 1500, 1900]),
            min_s=600,
            max_s=1800,
            video_duration_s=2400,
        )

        self.assertEqual(len(spans), 4)
        self.assertTrue(all(600 <= span["end"] - span["start"] <= 1800
                            for span in spans))
        self.assertTrue(all(
            spans[index]["end"] == spans[index + 1]["start"]
            for index in range(len(spans) - 1)
        ))

    def test_exact_scenario_removes_context_around_boundary(self) -> None:
        rows = self.rows([100, 300, 700, 1000, 1500, 1900])
        rows.append((1200, "phone", "phone", False, True))
        rows.sort(key=lambda row: row[0])

        spans = scenario_activity_spans(
            rows,
            min_s=600,
            max_s=1800,
            video_duration_s=2400,
        )

        self.assertTrue(all(
            span["end"] <= 1140 or span["start"] >= 1260
            for span in spans
        ))

    def test_exact_scenario_builds_inside_sensor_coverage(self) -> None:
        spans = scenario_activity_spans(
            self.rows([1000, 1300, 1800]),
            min_s=600,
            max_s=1800,
            video_duration_s=3000,
            allowed_intervals=[(900, 2100)],
        )

        self.assertEqual(len(spans), 2)
        self.assertGreaterEqual(spans[0]["start"], 900)
        self.assertLessEqual(spans[-1]["end"], 2100)


def _catalog_with_clips(
    parent: str,
    clips: tuple[tuple[str, float, float], ...],
) -> _Catalog:
    parent_s3 = f"s3://ego4d/{parent}.mp4"
    by_uid = {
        uid: {
            "exported_clip_uid": uid,
            "parent_video_uid": parent,
            "s3_path": f"s3://ego4d/clips/{uid}.mp4",
        }
        for uid, _start, _end in clips
    }
    videos = {
        parent: {
            "video_uid": parent,
            "s3_path": parent_s3,
            "device": "GoPro Hero Black 8",
            "scenarios": ["Gardening"],
        }
    }
    return _Catalog({}, videos, (), by_uid, {parent: clips})


class ExportedClipPreferenceTests(unittest.TestCase):
    def test_long_span_is_split_onto_official_clips(self) -> None:
        parent = "parent-a"
        catalog = _catalog_with_clips(parent, (
            ("clip-a", 0.0, 300.0),
            ("clip-b", 280.0, 600.0),
            ("clip-c", 600.0, 900.0),
        ))
        video = catalog.videos[parent]

        with patch("moneymin.ego4d._cat", return_value=catalog):
            records = _iter_span_records(
                video, {"start": 0.0, "end": 900.0, "match_score": 4},
                revalidate=_export_span_validator(
                    None, (), (), "", frozenset(),
                    min_dur_s=60, max_dur_s=1800, scenario=True))

        self.assertEqual(
            [(rec["window_s"], rec["media_uid"]) for rec in records],
            [((0.0, 300.0), "clip-a"),
             ((300.0, 600.0), "clip-b"),
             ((600.0, 900.0), "clip-c")],
        )
        self.assertTrue(all(_uses_exported_clip(rec) for rec in records))
        self.assertTrue(all(
            rec["s3_path"].startswith("s3://ego4d/clips/") for rec in records
        ))

    def test_span_without_official_clip_keeps_the_parent(self) -> None:
        parent = "parent-b"
        catalog = _catalog_with_clips(parent, ())
        video = catalog.videos[parent]

        with patch("moneymin.ego4d._cat", return_value=catalog):
            records = _iter_span_records(
                video, {"start": 10.0, "end": 700.0, "match_score": 1})

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["media_uid"], parent)
        self.assertEqual(records[0]["s3_path"], f"s3://ego4d/{parent}.mp4")
        self.assertFalse(_uses_exported_clip(records[0]))

    def test_prefer_long_clips_uses_exports_before_parent_video(self) -> None:
        clips = [
            {
                "clip_uid": "parent-cut",
                "parent_video_uid": "p1",
                "media_uid": "p1",
                "dur_s": 800,
            },
            {
                "clip_uid": "export-short",
                "parent_video_uid": "p2",
                "media_uid": "clip-x",
                "dur_s": 180,
            },
        ]

        ordered = prefer_long_clips(clips)

        self.assertEqual(
            [clip["clip_uid"] for clip in ordered],
            ["export-short", "parent-cut"],
        )

    def test_containing_export_preserves_whole_window_and_offset(self):
        catalog = _catalog_with_clips("p", (
            ("short", 100, 400), ("whole", 100, 1000)))
        validate = Mock(side_effect=AssertionError("unnecessary revalidation"))
        with patch.object(ego4d, "_cat", return_value=catalog):
            records = _iter_span_records(
                catalog.videos["p"], {"start": 100, "end": 1000},
                min_dur_s=600, revalidate=validate)
            restored, _ = ego4d.find_clip(records[0]["clip_uid"])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["window_s"], (100, 1000))
        self.assertEqual(records[0]["media_uid"], "whole")
        self.assertEqual(records[0]["media_time_offset_s"], 100)
        self.assertEqual(restored["media_uid"], "whole")
        self.assertEqual(restored["media_time_offset_s"], 100)

    def test_both_catalog_entrypoints_keep_minimum_when_exports_are_short(self):
        catalog = _catalog_with_clips("p", (
            ("a", 0, 300), ("b", 300, 600), ("c", 600, 900)))
        catalog.videos["p"]["duration_sec"] = 900
        rule = TaskRule(primary=("Gardening",), evidence=(("garden",),))
        span = {"start": 0, "end": 900, "match_score": 4}
        for scenario in (True, False):
            with self.subTest(scenario=scenario), ExitStack() as stack:
                stack.enter_context(patch.object(ego4d, "_cat", return_value=catalog))
                stack.enter_context(patch.object(ego4d, "load_timed_narrations",
                                                return_value={"p": ((400, "garden"),)}))
                stack.enter_context(patch.object(task_matching, "TASK_RULES", {"fixture": rule}))
                stack.enter_context(patch.object(task_matching, "rule_for", return_value=rule))
                stack.enter_context(patch.object(task_matching, "scenario_is_sufficient",
                                                return_value=scenario))
                stack.enter_context(patch.object(task_matching, "scenario_activity_spans",
                                                return_value=[span]))
                stack.enter_context(patch.object(task_matching, "extract_spans", return_value=[span]))
                single = ego4d.list_task_spans(
                    "fixture", min_dur_s=600, max_dur_s=1800, require_imu=False)
                ranked = ego4d.rank_all_task_spans(
                    min_dur_s=600, max_dur_s=1800, require_imu=False)["fixture"]
                for result in (single, ranked):
                    self.assertEqual([r["dur_s"] for r in result], [900])
                    self.assertEqual(result[0]["media_uid"], "p")

    def test_split_rechecks_evidence_and_keeps_only_locally_verified_piece(self):
        rule = TaskRule(primary=("Gardening",), evidence=(("garden",),))
        events = [(t, "#C C works in the garden") for t in range(300, 361, 10)]
        prepared = prepare_span_events(events)
        labels = task_matching.label_span_events(prepared, [("task", rule)])
        original = next(s for s in task_matching.extract_spans(
            rule, events, min_s=600, max_s=1800, activity_mode=True,
            video_duration_s=900) if s.get("expanded_from_verified_core"))
        catalog = _catalog_with_clips("p", (("empty", 0, 250), ("core", 250, 660)))
        validator = _export_span_validator(
            rule, prepared, labels, "task", frozenset(), min_dur_s=60, max_dur_s=1800)
        with patch.object(ego4d, "_cat", return_value=catalog):
            records = _iter_span_records(catalog.videos["p"], original, revalidate=validator)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["media_uid"], "core")
        self.assertEqual(records[0]["window_s"], (300, 360))
        self.assertEqual(len(records[0]["action_units"]), 7)

    def test_scenario_piece_reports_only_local_narrations(self):
        prepared = prepare_span_events([(400, "#C C works in the garden")])
        validator = _export_span_validator(
            None, prepared, (frozenset(),), "task", frozenset(),
            min_dur_s=60, max_dur_s=1800, scenario=True)
        self.assertEqual(validator(0, 250)[0]["action_units"], [])
        self.assertEqual(len(validator(250, 600)[0]["action_units"]), 1)

    def test_rejected_exports_preserve_valid_original(self):
        catalog = _catalog_with_clips("p", (("a", 0, 300), ("b", 300, 900)))
        with patch.object(ego4d, "_cat", return_value=catalog):
            records = _iter_span_records(
                catalog.videos["p"], {"start": 0, "end": 900}, revalidate=lambda s, e: [])
        self.assertEqual([(r["media_uid"], r["dur_s"]) for r in records], [("p", 900)])

    def test_short_nested_export_does_not_consume_valid_long_export(self):
        catalog = _catalog_with_clips("p", (("short", 0, 300), ("long", 0, 700)))
        with patch.object(ego4d, "_cat", return_value=catalog):
            records = _iter_span_records(
                catalog.videos["p"], {"start": 0, "end": 900}, min_dur_s=600,
                revalidate=_export_span_validator(None, (), (), "", frozenset(),
                    min_dur_s=600, max_dur_s=1800, scenario=True))
        self.assertEqual([(r["media_uid"], r["dur_s"]) for r in records], [("long", 700)])

    def test_priority_survives_parent_grouping_and_recognizes_legacy_exports(self):
        clips = [
            {"clip_uid": "export-p", "parent_video_uid": "p", "media_uid": "ep", "dur_s": 600},
            {"clip_uid": "export-q", "parent_video_uid": "q", "media_uid": "eq", "dur_s": 600},
            {"clip_uid": "parent-p", "parent_video_uid": "p", "media_uid": "p", "dur_s": 900},
            {"clip_uid": "legacy", "parent_video_uid": "p", "s3_path": "s3://fixture/legacy.mp4", "dur_s": 120},
        ]
        for shuffle in (False, True):
            ordered = prefer_long_clips(clips, shuffle=shuffle)
            self.assertEqual(ordered[-1]["clip_uid"], "parent-p")
            self.assertTrue(all(_uses_exported_clip(r) for r in ordered[:-1]))

    def test_export_validation_reuses_window_result(self):
        rule = TaskRule(primary=("Gardening",), evidence=(("garden",),))
        validator = _export_span_validator(
            rule, (), (), "task", frozenset(), min_dur_s=60, max_dur_s=1800)
        with patch.object(task_matching, "extract_spans", return_value=[]) as extract:
            validator(100, 300)
            validator(100, 300)
        extract.assert_called_once()


if __name__ == "__main__":
    unittest.main()
