"""New live task names preserve real activity boundaries in both providers."""
import unittest

from moneymin import nymeria, task_matching as tm


class LiveCatalogActivityBoundaryTests(unittest.TestCase):
    def providers(self):
        return (("Ego4D", tm.rule_for, tm.TASK_RULES),
                ("Nymeria", nymeria.selection_rule_for, nymeria.selection_rules()))

    def spans(self, name, events, lookup, rules):
        prepared = tm.prepare_span_events(events)
        labels = tm.label_span_events(prepared, rules.items())
        return tm.extract_spans(
            lookup(name), events, min_s=300, max_s=1800, pad_s=0,
            video_duration_s=events[-1][0], activity_mode=True,
            prepared_events=prepared, event_task_names=labels, task_name=name,
            competing_task_names=tm.competing_span_names(name, rules.items()),
            allowed_intervals=[(0, events[-1][0])])

    def test_food_storage_keeps_the_older_grocery_action_inside_one_take(self):
        events = [(float(t), "#C C puts groceries into the refrigerator")
                  for t in range(0, 421, 10)]
        for provider, lookup, rules in self.providers():
            with self.subTest(provider=provider):
                spans = self.spans("Putting Groceries & Food Away", events, lookup, rules)
                self.assertEqual([(s["start"], s["end"]) for s in spans], [(0, 420)])
                self.assertNotIn("Putting Groceries Away", tm.competing_span_names(
                    "Putting Groceries & Food Away", rules.items()))

    def test_brewing_and_resetting_the_station_share_the_same_take(self):
        events = [(float(t), "#C C grinds coffee beans and brews coffee" if t % 20 == 0
                   else "#C C rinses the coffee filter and wipes the coffee station")
                  for t in range(0, 421, 10)]
        for provider, lookup, rules in self.providers():
            with self.subTest(provider=provider):
                spans = self.spans("Brew Coffee or Tea", events, lookup, rules)
                self.assertEqual([(s["start"], s["end"]) for s in spans], [(0, 420)])
                self.assertNotIn("Drink Station Setup", tm.competing_span_names(
                    "Brew Coffee or Tea", rules.items()))

    def test_hand_washing_breaks_a_dishwasher_take(self):
        events = [(float(t), "#C C loads cups into the dishwasher" if t <= 420 or t >= 540
                   else "#C C washes dirty dishes by hand in a kitchen sink")
                  for t in range(0, 961, 10)]
        for provider, lookup, rules in self.providers():
            with self.subTest(provider=provider):
                spans = self.spans("Using the Dishwasher", events, lookup, rules)
                self.assertEqual([(s["start"], s["end"]) for s in spans],
                                 [(0, 420), (540, 960)])


if __name__ == "__main__":
    unittest.main()
