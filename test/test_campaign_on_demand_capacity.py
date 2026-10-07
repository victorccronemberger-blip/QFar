"""Estimates retain qualification and canonical source time for Nymeria."""
import unittest
from unittest.mock import patch

from moneymin import campaign_plan
from moneymin.campaign_types import AccountSpec, CampaignConfig, TaskSpec


class OnDemandCapacityTests(unittest.TestCase):
    def test_same_nymeria_footage_with_different_relative_origins_is_not_new_capacity(self):
        rows = [
            {"clip_uid": "nymeria-planned:sequence:110000000000:410000000000", "source": "nymeria",
             "parent_video_uid": "sequence", "window_s": [0, 300],
             "device_window_ns": [110000000000, 410000000000],
             "source_clock_domain": "aria_DEVICE_TIME_ns", "duration_s": 300,
             "task_id": "task", "eligible_accounts": ["fixture@example.invalid"],
             "acquisition_required": True, "requires_measured_validation": True},
            {"clip_uid": "nymeria:sequence:100:400", "source": "nymeria",
             "parent_video_uid": "sequence", "window_s": [100, 400],
             "device_window_ns": [110000000000, 410000000000],
             "source_clock_domain": "aria_DEVICE_TIME_ns", "duration_s": 300,
             "task_id": "other", "eligible_accounts": ["fixture@example.invalid"]},
        ]
        estimate = campaign_plan.capacity(rows, ["fixture@example.invalid"], target_seconds=600)
        self.assertEqual(estimate["accounts"][0]["unique_footage_upper_bound_seconds"], 300)
        self.assertEqual(estimate["estimated_sends"], 1)
        self.assertFalse(estimate["can_reach_goal"])
        self.assertEqual(estimate["pending_acquisition_clips"], 1)
        self.assertTrue(estimate["requires_measured_validation"])
        self.assertFalse(estimate["campaign_ready"])

    def test_public_review_does_not_promote_unacquired_sources_to_ready(self):
        clip = {"clip_uid": "nymeria-planned:sequence:110000000000:410000000000", "source": "nymeria",
                "parent_video_uid": "sequence", "window_s": [0, 300], "dur_s": 300,
                "device_window_ns": [110000000000, 410000000000],
                "source_clock_domain": "aria_DEVICE_TIME_ns", "window_origin_device_timestamp_ns": 110000000000,
                "capacity_kind": "annotation_estimate", "readiness": "pending_acquisition",
                "acquisition_required": True, "requires_measured_validation": True}
        cfg = CampaignConfig(accounts=[AccountSpec("fixture@example.invalid", "fixture-org")],
                             tasks=[TaskSpec("task", "scenario", 300, 1800)])
        with patch.object(campaign_plan.campaign, "automatic_candidates", return_value=[clip]), \
             patch.object(campaign_plan.sent_registry, "load", return_value={}):
            _pool, review, _fingerprint = campaign_plan.build(cfg)
        self.assertEqual(review[0]["readiness"], "pending_acquisition")
        self.assertEqual(review[0]["capacity_kind"], "annotation_estimate")
        self.assertTrue(review[0]["requires_measured_validation"])
        self.assertEqual(review[0]["device_window_ns"], clip["device_window_ns"])
