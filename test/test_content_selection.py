import unittest
from copy import deepcopy

from moneymin.content_selection import diverse_order, diversity_summary
from moneymin.task_matching import TaskRule, _activity_spans


class ContentSelectionTests(unittest.TestCase):
    def test_parents_alternate_without_dropping_or_changing_candidates(self):
        clips = [{"clip_uid": uid, "parent_video_uid": parent, "source": "ego4d"}
                 for uid, parent in (("a1", "a"), ("a2", "a"), ("a3", "a"),
                                     ("b1", "b"), ("b2", "b"), ("c1", "c"))]
        original = deepcopy(clips)
        ordered = diverse_order(clips)
        self.assertEqual([row["clip_uid"] for row in ordered], ["a1", "b1", "c1", "a2", "b2", "a3"])
        self.assertEqual(clips, original)
        self.assertEqual({id(row) for row in ordered}, {id(row) for row in clips})
        self.assertEqual(diversity_summary(clips)["parent_video_count"], 3)

    def test_same_parent_id_in_different_providers_is_distinct(self):
        clips = [{"parent_video_uid": "p", "source": source}
                 for source in ("ego4d", "ego4d", "holoassist")]
        self.assertEqual([row["source"] for row in diverse_order(clips)], ["ego4d", "holoassist", "ego4d"])
        self.assertEqual(diversity_summary(clips)["parent_video_count"], 2)

    def test_unseen_parent_precedes_more_cuts_from_already_used_parent(self):
        clips = [{"clip_uid": uid, "parent_video_uid": parent}
                 for uid, parent in (("a2", "a"), ("b1", "b"), ("a3", "a"))]
        result = diverse_order(clips, used_parents={("ego4d", "a")})
        self.assertEqual([row["clip_uid"] for row in result], ["b1", "a2", "a3"])

    def test_missing_parent_does_not_inflate_known_origins_or_drop_content(self):
        clips = [{"clip_uid": "one"}, {"clip_uid": "two"}, {}]
        self.assertEqual(len(diverse_order(clips)), 3)
        self.assertEqual(diversity_summary(clips)["parent_video_count"], 0)
        self.assertEqual(diversity_summary(clips)["unknown_parent_count"], 3)

    def test_brief_neutral_transition_does_not_discard_activity(self):
        rows = [(float(t), "garden", "garden", True, False) for t in range(0, 121, 10)]
        rows[6] = (60.0, "opens door", "opens door", False, False)
        spans = _activity_spans(TaskRule(primary=("Gardening",), evidence=(("garden",),)),
                                rows, min_s=60, max_s=180, max_gap_s=15, video_duration_s=120)
        self.assertEqual([(row["start"], row["end"]) for row in spans], [(0, 120)])
