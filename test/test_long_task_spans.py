from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
