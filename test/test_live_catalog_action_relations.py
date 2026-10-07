"""The acted-on object matters; a nearby object cannot supply task evidence."""
import unittest

from moneymin import nymeria, task_matching as tm


CASES = (
    ("Move furniture with someone (2+ people required)",
     "C and another person carry a sofa beside cardboard boxes",
     "C and another person carry boxes beside a sofa"),
    ("Receive a delivery at the door",
     "C accepts a package from a courier at the door and opens the box beside a coat rack",
     "C opens a cardboard box next to the door while a courier watches"),
    ("Clean and organize gym equipment",
     "C cleans gym equipment and puts weights back onto the rack beside a water bottle",
     "C cleans a table beside gym equipment and organizes papers"),
    ("Carry items up and down stairs",
     "C carries boxes upstairs past a lamp",
     "C carries a box past the stairs"),
    ("Serve food",
     "C brings food to another person at the table beside a flowerpot",
     "C puts plates on the table while a guest watches"),
    ("Putting Groceries & Food Away",
     "C puts groceries into the refrigerator near the dining table",
     "C places a box of groceries on a table beside the refrigerator"),
    ("Brew Coffee or Tea",
     "C brews coffee and cleans the coffee station beside a loaf of bread",
     "C prepares bread near the coffee maker and cleans the coffee station"),
    ("Clean and Polish Shoes",
     "C brushes shoes and applies polish beside a shoe rack",
     "C brushes and polishes a shoe rack next to a pair of boots"),
    ("Grill Food at a Barbecue",
     "C grills food on a barbecue beside a kitchen stove",
     "C cooks food on a stove at a barbecue party"),
    ("Pitch and Pack Up a Tent",
     "C assembles a camping tent beside a chair",
     "C assembles a chair beside a tent"),
    ("Replace an HVAC or Furnace Filter",
     "C removes the furnace filter and inserts a replacement near a vacuum cleaner",
     "C removes a filter from a vacuum cleaner beside the furnace"),
    ("Setting the Table",
     "C puts plates on the dining table beside a shelf",
     "C puts plates onto a shelf next to the dining table"),
    ("Set Up and Pack Away a Picnic",
     "C lays out a picnic blanket with food beside a bench",
     "C folds a blanket on the bed beside a picnic basket"),
    ("Using the Dishwasher",
     "C loads plates into the dishwasher beside a sponge",
     "C puts a sponge onto a plate beside the dishwasher"),
    ("Bathroom Clean & Tidy",
     "C organizes toiletries inside the bathroom next to a bookcase",
     "C organizes a bookcase outside the bathroom"),
)


class LiveCatalogActionRelationTests(unittest.TestCase):
    def test_action_is_bound_to_its_object_and_location_in_each_provider(self):
        for name, positive, adjacent in CASES:
            for provider, lookup in (("Ego4D", tm.rule_for),
                                     ("Nymeria", nymeria.selection_rule_for)):
                with self.subTest(task=name, provider=provider, caption=positive):
                    self.assertIsNotNone(tm.score_action(lookup(name), "#C " + positive))
                with self.subTest(task=name, provider=provider, caption=adjacent):
                    self.assertIsNone(tm.score_action(lookup(name), "#C " + adjacent))


if __name__ == "__main__":
    unittest.main()
