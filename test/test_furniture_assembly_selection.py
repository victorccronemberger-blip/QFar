"""Furniture evidence must describe the furniture, not games played on it."""
from __future__ import annotations

import unittest

from moneymin import task_matching


class FurnitureAssemblySelectionTests(unittest.TestCase):
    def labels(self, text):
        events = task_matching.prepare_span_events([(0, text)])
        return task_matching.label_span_events(events, task_matching.TASK_RULES.items())[0]

    def test_puzzles_and_rackets_are_not_furniture_assembly(self):
        texts = (
            "C assembles the puzzle pieces on the center table with both hands while kneeling.",
            "C picks up a jigsaw puzzle piece from the coffee table and attaches another piece.",
            "C holds a badminton racket, steps backward and hits the shuttlecock with the racket.",
            "C places board game pieces on the table and assembles the game parts.",
            "C arranges card game pieces on the table and picks up a part with both hands.",
            "C folds a piece of clothing on the bed and puts the piece on the bed.",
            "C attaches a party banner to the table with a piece of scotch tape.",
            "C stacks game pieces on the table with both hands while kneeling.",
            "C assembles Lego pieces with both hands while standing.",
            "C is standing in the building hallway cafeteria and arranges beverage sachets in a basket on the round table.",
            "C stands beside a table at a construction site and sorts snacks in a basket.",
        )
        for text in texts:
            with self.subTest(text=text):
                self.assertNotIn("Furniture Assembly", self.labels(text))
                self.assertNotIn("Furniture Assembly/ Disassembly", self.labels(text))

    def test_actual_furniture_and_jigsaw_tool_remain_valid(self):
        texts = (
            "C assembles a wooden table, attaching its legs and tightening bolts with a wrench.",
            "C cuts a shelf panel using a jigsaw tool and screws the wooden pieces onto the cabinet frame.",
            "C joins the bed frame parts and tightens the screws while standing.",
            "C assembles a table with both hands while standing.",
            "C assembles the chair and fastens its legs with bolts.",
            "C disassembles the metal shelf, unscrews the bolts and removes the shelf panels.",
            "C builds a wooden table using both hands while standing.",
            "C builds chair using both hands while standing.",
            "C is building the wardrobe using both hands while standing.",
            "C constructs a shelf from wooden panels while standing.",
        )
        for text in texts:
            with self.subTest(text=text):
                self.assertIn("Furniture Assembly", self.labels(text))
                self.assertIn("Furniture Assembly/ Disassembly", self.labels(text))

    def test_a_continuous_puzzle_is_not_an_assembly_window(self):
        name = "Furniture Assembly"
        rule = task_matching.rule_for(name)
        events = task_matching.prepare_span_events((index * 5,
            "C assembles puzzle pieces on the table with both hands while kneeling.") for index in range(91))
        labels = task_matching.label_span_events(events, task_matching.TASK_RULES.items())
        spans = task_matching.extract_spans(rule, (), min_s=300, max_s=1800,
            prepared_events=events, event_task_names=labels, task_name=name,
            competing_task_names=task_matching.competing_span_names(name, task_matching.TASK_RULES.items()),
            video_duration_s=455)
        self.assertEqual(spans, [])


if __name__ == "__main__":
    unittest.main()
