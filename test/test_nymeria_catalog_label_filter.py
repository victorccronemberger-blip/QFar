"""The catalog prefilter preserves every label from the full classifier."""
from contextlib import nullcontext
import random
import unittest
from unittest.mock import patch

from moneymin import ego4d, nymeria, nymeria_library, task_matching
import test_nymeria_catalog_batch as _batch_fixture


def _rule(*groups, **changes):
    return task_matching.TaskRule(primary=("fixture",), evidence=tuple(groups), **changes)


class CatalogLabelFilterTests(unittest.TestCase):
    def assert_same_labels(self, raw_events, rules):
        events = task_matching.prepare_span_events(raw_events)
        full = task_matching.label_span_events(events, rules.items())
        selected = nymeria_library._catalog_label_rules(events, iter(rules.items()))
        self.assertTrue(set(dict(selected)).issubset(rules))
        self.assertEqual(task_matching.label_span_events(events, selected), full)
        return events, full, dict(selected)

    def test_unit_threshold_precedes_whole_task_threshold_and_none_defaults(self):
        rules = {
            "low-unit": _rule(("lift",), ("table",), ("move",),
                              min_evidence_groups=3, unit_min_evidence_groups=1),
            "min-fallback": _rule(("lift",), ("table",), min_evidence_groups=1),
            "all-fallback": _rule(("lift",), ("table",)),
            "impossible": _rule(("garden",), ("water",)),
        }
        _events, labels, selected = self.assert_same_labels(
            [(0, "#C C lifts a box while standing")], rules)
        self.assertEqual(labels, (frozenset({"low-unit", "min-fallback"}),))
        self.assertIn("low-unit", selected)
        self.assertNotIn("all-fallback", selected)
        self.assertNotIn("impossible", selected)

    def test_zero_threshold_is_valid_and_empty_evidence_never_labels(self):
        rules = {
            "unit-zero": _rule(("absent",), min_evidence_groups=1, unit_min_evidence_groups=0),
            "min-zero": _rule(("absent",), min_evidence_groups=0),
            "empty-default": _rule(),
            "empty-zero": _rule(unit_min_evidence_groups=0),
        }
        _events, labels, selected = self.assert_same_labels([(0, "#C C opens a cupboard")], rules)
        self.assertEqual(labels, (frozenset({"unit-zero", "min-zero"}),))
        self.assertEqual(set(selected), {"unit-zero", "min-zero"})

    def test_phrase_prefix_and_word_boundaries_match_the_same_normalized_text(self):
        rules = {
            "bin": _rule(("bin",)),
            "cut-prefix": _rule(("cut",)),
            "phrase": _rule((" PICK   UP ",)),
            "joined-phrase": _rule(("garden plants",)),
            "empty-term": _rule(("", "   ")),
        }
        _events, labels, selected = self.assert_same_labels([
            (0, "#C C opens a cabinet"),
            (1, "#C C cuts cloth"),
            (2, "#C C will PICK UP a box"),
            # Joining events can retain an impossible phrase; this is safe.
            (3, "#C C waters garden"),
            (4, "#C plants are on the shelf"),
        ], rules)
        self.assertNotIn("bin", set().union(*labels))
        self.assertIn("cut-prefix", labels[1])
        self.assertIn("phrase", labels[2])
        self.assertIn("joined-phrase", selected)
        self.assertNotIn("joined-phrase", set().union(*labels))
        self.assertNotIn("empty-term", selected)

    def test_camera_wearer_segments_other_actor_and_unsure_are_preserved(self):
        rules = {"wearer-join": _rule(("cut grass",)), "other-actor": _rule(("waters",)),
                 "unsure": _rule(("unrecognized",))}
        events, labels, selected = self.assert_same_labels([
            (0, "#C cut #O someone waters plants #C grass"),
            (1, "#O someone waters plants"),
            (2, "#C unrecognized object #unsure"),
        ], rules)
        self.assertEqual(events[0][2], "cut grass")
        self.assertEqual(labels[0], frozenset({"wearer-join"}))
        self.assertFalse(labels[1])
        self.assertFalse(labels[2])
        self.assertEqual(set(selected), {"wearer-join"})

    def test_dirty_rows_do_not_supply_missing_evidence_or_remove_safe_labels(self):
        rules = {"garden": _rule(("garden",)), "water": _rule(("water",))}
        events, labels, selected = self.assert_same_labels([
            (0, "#C C waters plants while sitting"),
            (1, "#C C digs in the garden while standing"),
            (2, "#O another person is sitting #C C digs in the garden"),
        ], rules)
        self.assertTrue(events[0][3])
        self.assertFalse(labels[0])
        self.assertEqual(labels[1], frozenset({"garden"}))
        self.assertIn("garden", labels[2])
        self.assertNotIn("water", selected)
        _events, dirty_labels, dirty_selected = self.assert_same_labels(
            [(0, "#C C waters the garden while sitting")], rules)
        self.assertEqual(dirty_labels, (frozenset(),))
        self.assertEqual(dirty_selected, {})

    def test_required_action_and_exclusions_are_decided_per_event_after_filter(self):
        rules = {
            "scoped-exclusion": _rule(("garden",), action_excluded=("football",)),
            "required-action": _rule(("garden",), required_action_pattern=r"\bwaters\s+garden\b"),
            "required-absent": _rule(("garden",), required_action_pattern=r"\bpaints\s+garden\b"),
            "empty-regex-zero": _rule(("absent",), unit_min_evidence_groups=0,
                                      required_action_pattern=r""),
        }
        _events, labels, selected = self.assert_same_labels([
            (0, "#C C waters garden plants"),
            (1, "#C C looks at garden football equipment"),
            (2, "#C garden is outside"),
        ], rules)
        self.assertIn("scoped-exclusion", labels[0])
        self.assertNotIn("scoped-exclusion", labels[1])
        self.assertIn("required-action", labels[0])
        self.assertNotIn("required-action", labels[2])
        self.assertIn("required-absent", selected, "aggregate evidence only, not aggregate action gate")
        self.assertNotIn("required-absent", set().union(*labels))
        self.assertIn("empty-regex-zero", selected)

    def test_shared_groups_are_searched_once_and_duplicate_groups_still_count(self):
        repeated = ("lift", "carry")
        missing = ("garden",)
        rules = {f"shared-{index}": _rule(repeated, repeated, missing,
                                          unit_min_evidence_groups=2)
                 for index in range(20)}
        events = task_matching.prepare_span_events([(0, "#C C lifts a box while standing")])
        with patch.object(task_matching, "_evidence_group_present",
                          wraps=task_matching._evidence_group_present) as search:
            selected = nymeria_library._catalog_label_rules(events, rules.items())
            self.assertEqual(search.call_count, 2)
        self.assertEqual(set(dict(selected)), set(rules))
        self.assertEqual(task_matching.label_span_events(events, selected),
                         task_matching.label_span_events(events, rules.items()))

    def test_deterministic_random_events_and_real_rules_never_omit_a_label(self):
        rng = random.Random(12955)
        vocab = ("cut", "cuts", "cutting", "grass", "bin", "cabinet", "gardens", "garden",
                 "water", "waters", "table", "carry", "pick up", "box", "plants", "indoors",
                 "outdoor", "phone", "sitting", "football", "folding", "shirt", "#unsure")
        rules = nymeria.selection_rules()
        rules.update({
            "custom-unit": _rule(("cut", "carry"), ("table", "garden"), ("plants",),
                                 min_evidence_groups=3, unit_min_evidence_groups=1),
            "custom-zero": _rule(("absent",), unit_min_evidence_groups=0),
            "custom-action": _rule(("pick up",), required_action_pattern=r"\bpick\s+up\b"),
            "custom-prefix": _rule(("bin", "cut"), action_excluded=("football",)),
            "custom-empty": _rule(),
        })
        for scenario in range(80):
            rows = []
            for index in range(rng.randrange(1, 12)):
                text = " ".join(rng.choice(vocab) for _ in range(rng.randrange(1, 9)))
                prefix = rng.choice(("#C C ", "#O someone ", "", "#C "))
                if rng.randrange(4) == 0:
                    text += " #O " + rng.choice(vocab) + " #C " + rng.choice(vocab)
                rows.append((index * 5, prefix + text))
            with self.subTest(scenario=scenario):
                self.assert_same_labels(rows, rules)


class CatalogLabelPlanEquivalenceTests(unittest.TestCase):
    setUp = _batch_fixture.NymeriaCatalogBatchTests.setUp
    write_rows = _batch_fixture.NymeriaCatalogBatchTests.write_rows
    get_stream_id_from_label = _batch_fixture.NymeriaCatalogBatchTests.get_stream_id_from_label
    get_metadata = _batch_fixture.NymeriaCatalogBatchTests.get_metadata
    timestamps = _batch_fixture.NymeriaCatalogBatchTests.timestamps
    add_second_sequence = _batch_fixture.NymeriaCatalogBatchTests.add_second_sequence

    def matrix(self, *, full_classifier):
        nymeria.clear_caches()
        nymeria_library._EVENT_CACHE.clear()
        nymeria_library._CATALOG_INPUT_CACHE.clear()
        scope = (patch.object(nymeria_library, "_catalog_label_rules",
                              side_effect=lambda events, rules: tuple(rules))
                 if full_classifier else nullcontext())
        result = {}
        with scope, ego4d.selection_operation():
            for minimum, maximum in ((60, 1800), (300, 1800)):
                for name in nymeria.selection_rules():
                    result[(name, minimum, maximum)] = nymeria.planned_candidates(
                        task_name=name, min_dur_s=minimum, max_dur_s=maximum, root=self.root)
        return result

    def test_real_batch_plans_match_full_classifier_without_reusing_its_label_cache(self):
        self.add_second_sequence()
        full = self.matrix(full_classifier=True)
        filtered = self.matrix(full_classifier=False)
        self.assertTrue(any(full.values()))
        self.assertEqual(filtered, full)

    def test_60_then_300_reuses_short_negative_core_classification_once(self):
        rows = [(1000.0 + index * 5, 1005.0 + index * 5, _batch_fixture.ACTION)
                for index in range(6)]
        key = ("short-negative-core", nymeria._rules_digest())
        with patch.object(task_matching, "prepare_span_events",
                          wraps=task_matching.prepare_span_events) as prepare, \
             patch.object(task_matching, "label_span_events",
                          wraps=task_matching.label_span_events) as label:
            for minimum in (60, 300, 60):
                self.assertEqual(nymeria_library._catalog_windows(rows, [_batch_fixture.TASK],
                    minimum, 1800, key), [])
            self.assertEqual(prepare.call_count, 1)
            self.assertEqual(label.call_count, 1)
        self.assertNotIn(key, nymeria_library._EVENT_CACHE, "negative core needs no full rival pass")

    def positive_rows_with_rival(self):
        rows = [(1000.0 + index * 5, 1005.0 + index * 5, _batch_fixture.ACTION)
                for index in range(31)]
        rows.extend((1200.0 + index * 5, 1205.0 + index * 5,
                     "C assembles a wooden table with pieces and bolts while standing.")
                    for index in range(41))
        return rows

    def test_300_then_60_reuses_selected_labels_and_builds_full_rivals_when_needed(self):
        rows = self.positive_rows_with_rival()
        key = ("short-positive-core-and-rival", nymeria._rules_digest())
        with patch.object(task_matching, "prepare_span_events",
                          wraps=task_matching.prepare_span_events) as prepare, \
             patch.object(task_matching, "label_span_events",
                          wraps=task_matching.label_span_events) as label:
            self.assertEqual(nymeria_library._catalog_windows(rows, [_batch_fixture.TASK],
                300, 1800, key), [])
            self.assertEqual(prepare.call_count, 1)
            self.assertEqual(label.call_count, 1)
            self.assertNotIn(key, nymeria_library._EVENT_CACHE)
            recovered = nymeria_library._catalog_windows(rows, [_batch_fixture.TASK], 60, 1800, key)
            self.assertTrue(recovered)
            self.assertEqual(prepare.call_count, 1, "duration change must reuse prepared input")
            self.assertEqual(label.call_count, 2, "first possible core still needs full rival labels")
            events, full_labels = nymeria_library._EVENT_CACHE[key]
            self.assertTrue(any("Furniture Assembly" in names for names in full_labels))
            self.assertEqual(nymeria_library._catalog_windows(rows, [_batch_fixture.TASK],
                60, 1800, key), recovered)
            self.assertEqual(nymeria_library._catalog_windows(rows, [_batch_fixture.TASK],
                300, 1800, key), [])
            self.assertEqual(prepare.call_count, 1)
            self.assertEqual(label.call_count, 2)
        self.assertEqual(full_labels, task_matching.label_span_events(events, nymeria.selection_rules().items()))

    def test_changed_requested_names_do_not_reuse_another_negative_task_scope(self):
        rows = self.positive_rows_with_rival()
        key = ("shared-annotations-different-requested-tasks", nymeria._rules_digest())
        with patch.object(task_matching, "prepare_span_events",
                          wraps=task_matching.prepare_span_events) as prepare:
            self.assertEqual(nymeria_library._catalog_windows(rows, [_batch_fixture.TASK],
                300, 1800, key), [])
            recovered = nymeria_library._catalog_windows(rows, ["Furniture Assembly"], 60, 1800, key)
            self.assertTrue(recovered)
            self.assertTrue(all(row["task_name"] == "Furniture Assembly" for row in recovered))
            self.assertEqual(prepare.call_count, 2)
        self.assertIn((key, (_batch_fixture.TASK,)), nymeria_library._CATALOG_INPUT_CACHE)
        self.assertIn((key, ("Furniture Assembly",)), nymeria_library._CATALOG_INPUT_CACHE)
