import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moneymin import campaign, campaign_plan, ego4d, imu_coverage
from moneymin.campaign_types import AccountSpec, CampaignConfig, TaskSpec


class ImuCoverageTests(unittest.TestCase):
    def test_cuts_around_real_gaps_in_either_sensor_and_preserves_source_offset(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "parent_imu.csv"
            with source.open("w", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["canonical_timestamp_ms", "gyro_x", "gyro_y", "gyro_z", "accl_x", "accl_y", "accl_z"])
                for time in reversed(range(0, 12001, 10)):
                    accel = ["", "", ""] if 2000 < time < 2200 else [1, 2, 3]
                    gyro = ["", "", ""] if 8000 < time < 8500 else [1, 2, 3]
                    writer.writerow([time, *gyro, *accel])
            clip = {"clip_uid": "original", "exported_clip_uid": "original", "source": "ego4d",
                    "parent_video_uid": "parent", "window_s": (0, 12), "s3_path": "old",
                    "media_time_offset_s": 999, "dedup_clip_uids": ["official"]}
            def resolve(uid):
                _, start, end = uid.rsplit("_", 2)
                return {"exported_clip_uid": uid, "parent_video_uid": "parent", "parent_start_sec": start,
                        "parent_end_sec": end, "s3_path": "actual", "media_uid": "source", "media_time_offset_s": 0,
                        "needs_cut": True}, {}
            with patch.object(imu_coverage.config, "DATA_DIR", root), \
                 patch.object(ego4d, "find_clip", side_effect=resolve), patch.object(ego4d, "_action_index", return_value={}):
                result = imu_coverage.refine_candidates([clip], root, 3, 12)
                imu_coverage._intervals.cache_clear()
                with patch("moneymin.imu_coverage.csv.DictReader", side_effect=AssertionError("cached source reread")):
                    self.assertEqual(imu_coverage.refine_candidates([clip], root, 3, 12), result)
            self.assertEqual(len(result), 2)
            for row in result:
                self.assertEqual(row["exported_clip_uid"], row["clip_uid"])
                self.assertEqual(row["media_time_offset_s"], 0)
                self.assertEqual(row["dedup_clip_uids"], ["official", "original"])
                ego4d.build_imu_csv(source, row["window_s"], validate_only=True)
            # A changed source must not reuse the old continuity result.
            source.write_text("broken", encoding="utf-8")
            self.assertEqual(imu_coverage.refine_candidates([clip], root, 3, 12), [clip])

    def test_refined_window_inherits_sent_and_pending_exclusions(self):
        clip = {"clip_uid": "cut", "dedup_clip_uids": ["original"], "dur_s": 300}
        cfg = CampaignConfig([AccountSpec("sent@example.com", "org"), AccountSpec("pending@example.com", "org"),
                              AccountSpec("fresh@example.com", "org")], [TaskSpec("task", "scenario", 300, 600)],
                             recovery_exclusions={"original": ["pending@example.com"]})
        with patch.object(campaign, "automatic_candidates", return_value=[clip]), \
             patch.object(campaign.sent_registry, "load", return_value={"scenario": {"original": ["sent@example.com"]}}):
            _, review, _ = campaign_plan.build(cfg)
            self.assertEqual(campaign._candidate_sent_emails("scenario", clip), {"sent@example.com"})
            self.assertEqual(campaign._candidate_reserved_emails(cfg, clip), {"pending@example.com"})
        self.assertEqual(review[0]["eligible_accounts"], ["fresh@example.com"])
        self.assertEqual(review[0]["pending_accounts"], ["pending@example.com"])
