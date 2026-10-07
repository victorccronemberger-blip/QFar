"""Preview does not acquire or scan source media; runtime retains its gates."""
import unittest
from unittest.mock import patch

from moneymin import campaign, campaign_plan, imu_coverage
from moneymin.campaign_types import AccountSpec, CampaignConfig, TaskSpec


class CatalogPreflightTests(unittest.TestCase):
    def setUp(self):
        self.task = TaskSpec("task", "fixture", 300, 1800, task_name="Task")
        self.cfg = CampaignConfig([AccountSpec("fixture@example.invalid", "org")],
                                  [self.task], dataset_provider="ambos", content_mode="dataset")
        self.clip = {"clip_uid": "fixture", "source": "ego4d", "dur_s": 300}

    def test_preview_and_execution_use_distinct_sensor_validation_paths(self):
        with patch.object(campaign.task_matching, "rule_for", return_value=object()), \
             patch.object(campaign, "_compatible_task_clips", return_value=[self.clip]), \
             patch.object(campaign.nymeria, "automatic_candidates", return_value=[]) as nymeria, \
             patch.object(imu_coverage, "refine_candidates", return_value=[self.clip]) as refine:
            preview = campaign.automatic_candidates(self.task, self.cfg, catalog_only=True)
            refine.assert_not_called()
            self.assertTrue(nymeria.call_args.kwargs["catalog_only"])
            self.assertTrue(preview[0]["requires_measured_validation"])
            self.assertEqual(preview[0]["capacity_kind"], "estimate_before_preparation")
            runtime = campaign.automatic_candidates(self.task, self.cfg)
            refine.assert_called_once()
            self.assertNotIn("catalog_only", nymeria.call_args.kwargs)
            self.assertEqual(runtime, [self.clip])
            self.assertNotIn("requires_measured_validation", self.clip)

    def test_preview_cache_filter_never_opens_media(self):
        self.cfg.content_mode = "cache"
        self.cfg.dataset_provider = "ego4d"
        with patch.object(campaign.task_matching, "rule_for", return_value=object()), \
             patch.object(campaign, "_compatible_task_clips", return_value=[self.clip]), \
             patch.object(campaign, "_with_cached_expansion", return_value=[self.clip]) as expansion, \
             patch.object(campaign, "_catalog_clip_cached_hint", return_value=True) as hint, \
             patch.object(campaign, "_clip_is_cached", side_effect=AssertionError("media opened")):
            self.assertEqual(len(campaign.automatic_candidates(self.task, self.cfg, catalog_only=True)), 1)
            self.assertTrue(expansion.call_args.kwargs["catalog_only"])
            hint.assert_called_once_with(self.clip, self.cfg.work_dir)

    def test_catalog_plan_preserves_exclusions_and_reviewed_identity(self):
        self.cfg.recovery_exclusions = {"fixture": ["fixture@example.invalid"]}
        with patch.object(campaign, "automatic_candidates", return_value=[self.clip]) as pool, \
             patch.object(campaign_plan.sent_registry, "load", return_value={}):
            planned, review, _ = campaign_plan.build(self.cfg, catalog_only=True)
        pool.assert_called_once_with(self.task, self.cfg, catalog_only=True)
        self.assertEqual(review[0]["eligible_accounts"], [])
        self.assertEqual(review[0]["pending_accounts"], ["fixture@example.invalid"])
        self.cfg.candidate_plan = planned
        with patch.object(imu_coverage, "refine_candidates", side_effect=AssertionError("review replaced")):
            self.assertEqual(campaign.automatic_candidates(self.task, self.cfg), [self.clip])
