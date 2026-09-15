import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from moneymin import campaign, task_matching as tm


class CurrentTaskNamesTests(unittest.TestCase):
    def test_all_43_live_account_task_names_are_supported(self):
        names = json.loads(Path(__file__).with_name("current_minute_task_names.json")
                           .read_text(encoding="utf-8"))
        self.assertEqual(len(names), 43)
        for name in names:
            with self.subTest(name=name):
                self.assertIsNotNone(tm.rule_for(name))
                self.assertIn(tm.canonical_task_name(name), tm.TASK_RULES)

    def test_current_names_map_to_known_actions(self):
        names = [
            "Cleaning Car", "Planting or Pulling Weeds", "Shopping",
            "Folding Clothes or Putting Them on Hangers", "Using the Laundry Machine",
            "Trim Hedges and Branches", "Bathroom Deep Clean", "Watering Outdoor Plants",
            "Taking Out Trash", "Replace Bulbs or Batteries", "Sweep the Porch",
            "Hand Washing Clothes", "Putting Groceries Away", "Bedroom Deep Clean",
            "Organize the Garage", "Hotel Laundry Operations", "Running Industrial Laundry",
            "Pump Gas", "Shoveling Snow", "Toy or Clothing Pickup",
            "Pack or Unpack a Car for a Trip", "Leaf Raking or Blowing",
            "Spreading Mulch or Fertilizer", "Tighten Cabinet or Door Hinges",
        ]
        for name in names:
            with self.subTest(name=name):
                self.assertIsNotNone(tm.rule_for(name))
                self.assertIn(tm.canonical_task_name(name), tm.TASK_RULES)

    def test_case_and_alias_use_ranked_pool_without_reclassifying_seed(self):
        clip = {"clip_uid": "accepted", "dur_s": 600}
        with patch.object(campaign.ego4d, "has_timed_narrations", return_value=True), \
             patch.object(campaign, "_duration_ranked_pools", return_value={"Trim a hedge": (clip,)}), \
             patch.object(campaign, "_task_candidates", side_effect=AssertionError("lost canonical pool")):
            for name in ("Trim Hedges and Branches", " trim a HEDGE "):
                self.assertEqual(campaign._compatible_task_clips(
                    name, "ego4d", min_dur_s=300, max_dur_s=1800), (clip,))

    def test_planting_is_not_all_gardening(self):
        rule = tm.rule_for("Planting or Pulling Weeds")
        for text in ("#C C waters flowers in the garden", "#C C harvests tomatoes",
                     "#C C trims the hedge", "#O O plants seedlings",
                     "#C C throws the plants", "#C C touches the plants",
                     "#C C cuts the plants with the hand pruner"):
            self.assertIsNone(tm.score_action(rule, text))
        for text in ("#C C plants seedlings in the soil", "#C C pulls weeds from the bed"):
            self.assertIsNotNone(tm.score_action(rule, text))

    def test_folding_and_hanging_both_match_but_washing_does_not(self):
        rule = tm.rule_for("Folding Clothes or Putting Them on Hangers")
        for text in ("#C C folds a shirt", "#C C hangs clothes on hangers"):
            self.assertIsNotNone(tm.score_action(rule, text))
        self.assertIsNone(tm.score_action(rule, "#C C scrubs clothes in a basin"))

    def test_putting_folded_clothes_on_bed_is_not_making_bed(self):
        rule = tm.rule_for("Change Sheets & Make Bed")
        for text in ("#C C puts a cloth on a bed", "#C C puts the towel on the bed",
                     "#C C puts the folded shirt on the bed"):
            self.assertIsNone(tm.score_action(rule, text))
        for text in ("#C C makes the bed", "#C C puts a bedsheet on the mattress"):
            self.assertIsNotNone(tm.score_action(rule, text))

    def test_folding_session_survives_clothes_placed_on_bed(self):
        name = "Folding Clothes or Putting Them on Hangers"
        rules = [(n, tm.rule_for(n)) for n in (name, "Change Sheets & Make Bed")]
        events = [(float(t), "#C C folds a shirt" if t % 20 == 0 else
                   "#C C puts a cloth on the bed") for t in range(0, 421, 10)]
        prepared = tm.prepare_span_events(events)
        spans = tm.extract_spans(tm.rule_for(name), events, min_s=300,
            max_s=1800, pad_s=0, video_duration_s=420, activity_mode=True,
            prepared_events=prepared, task_name=name,
            event_task_names=tm.label_span_events(prepared, rules),
            competing_task_names=tm.competing_span_names(name, rules),
            allowed_intervals=[(0, 420)])
        self.assertTrue(spans)
        self.assertTrue(any(s["end"] - s["start"] >= 300 for s in spans))

    def test_new_machine_task_accepts_both_directions(self):
        rule = tm.rule_for("Using the Laundry Machine")
        for text in ("#C C puts clothes into the washing machine",
                     "#C C removes clothes from the dryer"):
            self.assertIsNotNone(tm.score_action(rule, text))
        self.assertIsNone(tm.score_action(rule, "#C C cleans the washing machine"))

    def test_specific_actions_are_not_competing_with_their_combined_task(self):
        for parent, child in (
                ("Planting or Pulling Weeds", "Pull weeds by hand"),
                ("Cleaning Car", "Car Wash & Detail"),
                ("Using the Laundry Machine", "Loading the Laundry Machine")):
            self.assertNotIn(child, tm.competing_span_names(parent, tm.TASK_RULES.items()))
            self.assertNotIn(parent, tm.competing_span_names(child, tm.TASK_RULES.items()))

    def test_unknown_server_task_is_visible_as_unsupported(self):
        session = Mock(_live=True)
        session.all_tasks.return_value = [{"id": "new", "name": "A future task"}]
        result = campaign.available_tasks("unused", "org", session=session,
                                           min_dur_s=300, max_dur_s=1800,
                                           include_unavailable=True)
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0]["mapping_supported"])
        self.assertFalse(result[0]["available_for_duration"])


if __name__ == "__main__":
    unittest.main()
