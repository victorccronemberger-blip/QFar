import unittest

from moneymin.web.server import _parse_duration_range


class CampaignDurationRangeTests(unittest.TestCase):
    def test_accepts_user_selected_minimum_and_maximum(self):
        minimum, maximum = _parse_duration_range(
            {"min_dur_s": 5 * 60, "max_dur_s": 12 * 60}
        )

        self.assertEqual(minimum, 300)
        self.assertEqual(maximum, 720)

    def test_keeps_legacy_one_minute_default(self):
        minimum, maximum = _parse_duration_range({"max_dur_s": 10 * 60})

        self.assertEqual(minimum, 60)
        self.assertEqual(maximum, 600)

    def test_rejects_minimum_greater_than_maximum(self):
        with self.assertRaisesRegex(ValueError, "não pode exceder"):
            _parse_duration_range({"min_dur_s": 15 * 60, "max_dur_s": 10 * 60})


if __name__ == "__main__":
    unittest.main()
