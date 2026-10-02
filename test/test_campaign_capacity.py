import unittest

from moneymin.campaign_plan import available_seconds


class CampaignCapacityTests(unittest.TestCase):
    def row(self, uid, start, end, emails=("a", "b"), parent="parent"):
        return dict(clip_uid=uid, parent_video_uid=parent, window_s=[start, end],
                    duration_s=end-start, eligible_accounts=list(emails))

    def test_shared_and_overlapping_source_windows_are_not_added_twice(self):
        rows = [self.row("one", 0, 300), self.row("alias", 0, 300),
                self.row("overlap", 150, 450), self.row("other", 0, 300, parent="other")]
        self.assertEqual(available_seconds(rows, ["a", "b"]), {"a": 750, "b": 750})

    def test_history_and_pending_exclusions_remain_per_account(self):
        rows = [self.row("one", 0, 300, ("a",)), self.row("two", 150, 450, ("b",)),
                self.row("three", 450, 600, ())]
        self.assertEqual(available_seconds(rows, ["a", "b", "c"]), {"a": 300, "b": 300, "c": 0})

    def test_clips_without_parent_windows_are_deduplicated_by_identity(self):
        rows = [dict(clip_uid="holoassist:one", duration_s=120, eligible_accounts=["a"])]*2
        self.assertEqual(available_seconds(rows, ["a"]), {"a": 120})

    def test_non_finite_duration_cannot_inflate_the_preview(self):
        for duration in (float("nan"), float("inf"), -1, 0):
            row = dict(clip_uid="bad", duration_s=duration, eligible_accounts=["a"])
            self.assertEqual(available_seconds([row], ["a"]), {"a": 0})
