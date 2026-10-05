"""NymeriaPlus provider: index + IMU resample + prepare wiring."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

NYMERIA_ROOT = Path(r"C:\Users\victo\OneDrive\Desktop\NymeriaPlus")


@unittest.skipUnless(
    (NYMERIA_ROOT / "20230607_s1_barbara_wheeler_act4_5adv12" / "metadata.json").is_file(),
    "sequência piloto NymeriaPlus ausente",
)
class NymeriaProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["NYMERIA_ROOT"] = str(NYMERIA_ROOT)

    def test_list_sequences_and_windows(self):
        from moneymin import nymeria
        seqs = nymeria.list_sequences()
        self.assertGreaterEqual(len(seqs), 1)
        wins = nymeria.list_windows(seqs[0], min_dur_s=60, max_dur_s=60)
        self.assertTrue(wins)
        self.assertTrue(wins[0]["clip_uid"].startswith("nymeria:"))
        self.assertEqual(wins[0]["source"], "nymeria")

    def test_imu_csv_500hz_and_span_pin(self):
        from moneymin import nymeria, nymeria_vrs
        seq = nymeria.list_sequences()[0]
        t0, t1 = nymeria.device_window_for_sequence(
            Path(seq["path"]), start_s=0.0, end_s=2.0)
        samples = nymeria_vrs.read_imu_samples(
            Path(seq["path"]) / "recording_head" / "data" / "motion.vrs",
            t0_ns=t0, t1_ns=t1)
        stats: dict = {}
        csv = nymeria_vrs.build_imu_csv_from_samples(samples, stats=stats)
        self.assertTrue(csv.startswith("t,ax,ay,az,wx,wy,wz\n"))
        self.assertEqual(stats["maxInterpolationSpanNs"], "25000000")
        self.assertEqual(stats["strategy"], "gyro_anchored_v1")
        self.assertGreaterEqual(stats["sampleCount"], 500)

    def test_normalize_dataset_provider_accepts_nymeria(self):
        from moneymin import campaign
        self.assertEqual(campaign.normalize_dataset_provider("nymeria"), "nymeria")
        self.assertEqual(campaign.normalize_dataset_provider("all"), "all")

    def test_automatic_candidates_include_nymeria(self):
        from moneymin import campaign
        from moneymin.campaign_types import CampaignConfig, TaskSpec
        cfg = CampaignConfig(
            accounts=[],
            tasks=[TaskSpec(task_id="t", task_name="Gardening", scenario="Gardening",
                            count=1, min_dur_s=60, max_dur_s=120)],
            work_dir=tempfile.mkdtemp(prefix="nymeria-cand-"),
            dataset_provider="nymeria",
        )
        # Avoid Ego4D refine requiring catalog
        with patch.object(campaign, "normalize_content_mode", return_value="dataset"):
            cands = campaign.automatic_candidates(cfg.tasks[0], cfg)
        self.assertTrue(any(str(c.get("clip_uid", "")).startswith("nymeria:") for c in cands))


if __name__ == "__main__":
    unittest.main()
