"""The live organization tasks require the action in their published description."""
from __future__ import annotations

import json
from pathlib import Path
import unittest

from moneymin import task_matching as tm


POSITIVE_ACTIONS = {
    "Move furniture with someone (2+ people required)":
        "C and another man lift and carry the sofa to a new spot together.",
    "Receive a delivery at the door":
        "C answers the door, accepts a delivery package, brings it inside and unboxes the package.",
    "Clean and organize gym equipment":
        "C wipes down gym equipment and puts dumbbells back onto the rack.",
    "Carry items up and down stairs":
        "C carries a box up the stairs and brings the box downstairs.",
    "Serve food":
        "C brings a plate of food to the woman at the dining table.",
    "Putting Groceries & Food Away":
        "C organizes food in the pantry and transfers vegetables into the fridge.",
    "Brew Coffee or Tea":
        "C brews coffee and wipes the coffee station after washing the mug.",
    "Clean and Polish Shoes":
        "C brushes dirt off the shoes, applies shoe polish and buffs the shoes.",
    "Grill Food at a Barbecue":
        "C flips the chicken with tongs on the barbecue grill.",
    "Pitch and Pack Up a Tent":
        "C dismantles a tent and packs it into its bag.",
    "Replace an HVAC or Furnace Filter":
        "C removes the old furnace filter and inserts a new HVAC filter.",
    "Setting the Table":
        "C places plates, glasses, forks and napkins on the dining table.",
    "Set Up and Pack Away a Picnic":
        "C unpacks a picnic basket and lays out food and tableware on a blanket.",
    "Using the Dishwasher":
        "C loads dirty plates and cups into the dishwasher and starts the machine.",
    "Bathroom Clean & Tidy":
        "C organizes toiletries in the bathroom and wipes the shower.",
}


class LiveMinuteTaskRulesTests(unittest.TestCase):
    def test_every_captured_live_task_has_a_rule(self):
        path = Path(__file__).with_name("current_minute_task_names.json")
        names = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(names), 49)
        for name in names:
            with self.subTest(task=name):
                self.assertIsNotNone(tm.rule_for(name))

    def test_new_published_actions_are_proven(self):
        for name, text in POSITIVE_ACTIONS.items():
            with self.subTest(task=name):
                rule = tm.rule_for(name)
                self.assertIsNotNone(tm.score_action(rule, "#C " + text))
                self.assertIsNotNone(tm.score_narrated_scenarios(name, rule, ("Cooking",)))

    def test_other_actor_cannot_supply_the_new_actions(self):
        for name, text in POSITIVE_ACTIONS.items():
            with self.subTest(task=name):
                self.assertIsNone(tm.score_action(tm.rule_for(name), "#O " + text))

    def test_furniture_requires_someone_participating_in_the_move(self):
        name = "Move furniture with someone (2+ people required)"
        for text in (
            "C carries the chair alone to another room.",
            "C moves the chair and table together.",
            "C moves the sofa while another man watches.",
            "C carries a chair and talks to another person.",
        ):
            with self.subTest(text=text):
                self.assertIsNone(tm.score_action(tm.rule_for(name), "#C " + text))

    def test_serving_requires_food_and_a_recipient_at_the_table(self):
        for text in (
            "C brings an empty plate to another person at the table.",
            "C puts food on the table for himself.",
            "C cooks rice in a pot and puts a plate on the counter.",
            "C puts plates on the table while a guest watches.",
            "C puts a plate on the table next to a guest.",
            "C puts food on the table while a guest watches.",
        ):
            with self.subTest(text=text):
                self.assertIsNone(tm.score_action(tm.rule_for("Serve food"), "#C " + text))

    def test_grill_and_tent_actions_apply_to_the_named_object(self):
        negatives = {
            "Grill Food at a Barbecue": (
                "C puts food on a plate for a barbecue guest.",
                "C cooks chicken on the kitchen stove.",
                "C cooks food on a stove at a barbecue party.",
            ),
            "Pitch and Pack Up a Tent": (
                "C assembles a chair beside a tent.",
                "C folds a shirt beside a tent.",
            ),
        }
        for name, texts in negatives.items():
            for text in texts:
                with self.subTest(task=name, text=text):
                    self.assertIsNone(tm.score_action(tm.rule_for(name), "#C " + text))

    def test_brewing_requires_preparation_and_reset_of_the_station(self):
        for text in (
            "C drinks coffee from a mug.",
            "C brews coffee into the cup.",
            "C brews coffee and then cleans the kitchen stove.",
        ):
            with self.subTest(text=text):
                self.assertIsNone(tm.score_action(tm.rule_for("Brew Coffee or Tea"), "#C " + text))
        units = ["C brews coffee into a mug", "C wipes the coffee station"]
        self.assertIsNotNone(tm.score_action(tm.rule_for("Brew Coffee or Tea"), " ".join(units), units))

    def test_dishwasher_is_required_for_machine_task(self):
        for text in (
            "C washes dishes in the sink and puts plates away.",
            "C puts dishes onto a rack beside the sink.",
            "C puts a sponge onto a plate beside the dishwasher.",
        ):
            with self.subTest(text=text):
                self.assertIsNone(tm.score_action(tm.rule_for("Using the Dishwasher"), "#C " + text))

    def test_real_coffee_machine_operations_and_reset_prove_both_phases(self):
        rule = tm.rule_for("Brew Coffee or Tea")
        units = (
            "C puts down the coffee pod into the coffee pod bin of the coffee machine, "
            "then pushes down the coffee machine lever and places a mug on the tray.",
            "C presses a button on the coffee machine with her right hand.",
            "C removes the coffee pod in the coffee maker and puts it on kitchen tissue.",
            "C washes the mug with water and closes the faucet.",
        )
        for text in units:
            with self.subTest(text=text):
                self.assertTrue(tm._unit_on_task(text.casefold(), rule))
        self.assertIsNotNone(tm.score_action(rule, " ".join(units), units))
        self.assertIsNone(tm.score_action(rule, " ".join(units[:2]), units[:2]))
        self.assertIsNone(tm.score_action(rule, "C drinks coffee and washes the mug."))
        events = tuple((float(t), "#C " + (units[1] if t < 150 else
                        ("C listens to her peer." if t < 300 else units[3])))
                       for t in range(0, 451, 5))
        self.assertEqual(tm.extract_spans(rule, events, min_s=300, max_s=1800), [])

    def test_storage_area_organization_and_holiday_takedown_are_valid(self):
        self.assertIsNotNone(tm.score_action(tm.rule_for("Organize the Garage"),
            "#C C organizes boxes and shelves in the storage area."))
        self.assertIsNone(tm.score_action(tm.rule_for("Organize the Garage"),
            "#C C walks beside shelves and clutter in the storage area."))
        self.assertIsNotNone(tm.score_action(tm.rule_for("Holiday Decoration Setup"),
            "#C C takes down the Christmas decorations and packs away the ornaments."))
        self.assertIsNotNone(tm.score_action(tm.rule_for("Hang Curtains"),
            "#C C installs the window bracket and hangs the blinds."))

    def test_wrong_filter_and_personal_hygiene_are_not_household_tasks(self):
        self.assertIsNone(tm.score_action(tm.rule_for("Replace an HVAC or Furnace Filter"),
            "#C C removes the coffee filter from the coffee machine."))
        self.assertIsNone(tm.score_action(tm.rule_for("Bathroom Clean & Tidy"),
            "#C C washes his face at the bathroom sink."))


if __name__ == "__main__":
    unittest.main()
