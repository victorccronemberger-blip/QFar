"""Campanha Ego4D: narração local, download e prova antes do preparo."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from moneymin import campaign, config, ego4d
from moneymin.campaign_types import AccountSpec, CampaignConfig, TaskSpec
from moneymin.web.runner import CampaignRunner, friendly_campaign_error

PARENT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TASK = "Folding Clothes or Putting Them on Hangers"
UNPROVEN = "portable-unproven-window"


def _events() -> str:
    rows = []
    for second in range(0, 601, 5):
        rows.append(json.dumps({
            "video_uid": PARENT,
            "events": [[second, "#C C folds the shirt"]],
        }))
    # One record with the full timeline. A line per instant also works, but
    # the parser keeps the last complete record for a video only if each line
    # is its own object. Emit one object that contains every event.
    return json.dumps({
        "video_uid": PARENT,
        "events": [[second, "#C C folds the shirt"] for second in range(0, 601, 5)],
    }) + "\n"


class NarratedCampaignCatalogTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        library = self.root / "library" / "ego4d"
        library.mkdir(parents=True)
        (library / "ego4d.json").write_text(json.dumps({
            "version": "fixture",
            "videos": [{
                "video_uid": PARENT,
                "duration_sec": 700,
                "scenarios": ["Cleaning / laundry"],
                "has_imu": True,
                "s3_path": "s3://fixture/parent.mp4",
            }],
        }), encoding="utf-8")
        (library / "clips.csv").write_text(
            "exported_clip_uid,parent_video_uid,parent_start_sec,parent_end_sec,s3_path\n",
            encoding="utf-8")
        (library / "timed_narrations.jsonl").write_text(_events(), encoding="utf-8")
        self.library = library
        self.stack.enter_context(patch.object(config, "DATA_DIR", self.root))
        self.stack.enter_context(patch.object(config, "MEDIA_DATA_DIR", self.root / "library"))
        self.stack.enter_context(patch.object(ego4d, "EGO4D_DIR", library))
        campaign._RANK_INPUT_SIGNATURE = None
        for func in (
            campaign._rank_cache_stamp, campaign._ranked_pools_cached,
            campaign._duration_ranked_pools, campaign._load_rank_seed,
            campaign._task_candidates, ego4d._timed_bytes_cached,
            ego4d._catalog_bytes_cached,
        ):
            func.cache_clear()
            self.addCleanup(func.cache_clear)
        self.addCleanup(setattr, campaign, "_RANK_INPUT_SIGNATURE", None)
        real_seed = campaign._load_rank_seed()

        def seeded():
            base = {name: tuple(rows) for name, rows in (real_seed or {}).items()}
            extra = {
                "clip_uid": UNPROVEN,
                "parent_video_uid": "unproven-parent-not-in-library",
                "window_s": [0.0, 120.0],
                "dur_s": 120.0,
                "s3_path": "s3://fixture/unproven.mp4",
                "source": "ego4d",
                "needs_cut": True,
            }
            base[TASK] = tuple(base.get(TASK, ())) + (extra,)
            return base

        seeded.cache_clear = lambda: None
        self.stack.enter_context(patch.object(campaign, "_load_rank_seed", seeded))
        campaign._rank_cache_path().write_text(json.dumps({
            "schema": 2,
            "stamp": [
                ["ego4d.json", 1, "old"],
                ["clips.csv", 1, "old"],
                ["clip_narrations.json", 0, "missing"],
                ["timed_narrations.jsonl", 0, "missing"],
            ],
            "buckets": {TASK: []},
        }), encoding="utf-8")

    def narrated_rows(self):
        rows = campaign.available_tasks(
            "fixture@example.invalid", "org",
            remote_tasks=[{"id": "fold", "name": TASK}],
            dataset_provider="ego4d", content_mode="dataset",
            min_dur_s=60, max_dur_s=1800, include_unavailable=True)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def narrated_clip(self, mode):
        spec = TaskSpec("fold", "Cleaning / laundry", 60, 1800, task_name=TASK, count=1)
        cfg = CampaignConfig(
            accounts=[AccountSpec("fixture@example.invalid", "org")],
            tasks=[spec],
            work_dir=self.library,
            dataset_provider="ego4d",
            content_mode=mode,
            cleanup_after_upload=True,
        )
        return campaign.automatic_candidates(spec, cfg)

    def test_narrated_clip_survives_stale_cache_and_cache_mode_does_not(self):
        first = self.narrated_rows()
        second = self.narrated_rows()
        self.assertGreater(first["clip_count"], 0)
        self.assertEqual(first["clip_count"], second["clip_count"])
        dataset = self.narrated_clip("dataset")
        both = self.narrated_clip("both")
        cached = self.narrated_clip("cache")
        def parents(rows):
            return {row.get("parent_video_uid") for row in rows}
        self.assertIn(PARENT, parents(dataset))
        self.assertIn(PARENT, parents(both))
        self.assertNotIn(PARENT, parents(cached))
        chosen = next(row for row in dataset if row.get("parent_video_uid") == PARENT)
        self.assertFalse(any(self.library.glob("*.mp4")))
        self.assertNotIn(UNPROVEN, {row.get("clip_uid") for row in dataset})
        self.assertNotIn(UNPROVEN, {row.get("clip_uid") for row in both})

    def test_selected_narration_revalidates_and_unproven_seed_stays_out(self):
        dataset = self.narrated_clip("dataset")
        chosen = next(row for row in dataset if row.get("parent_video_uid") == PARENT)
        ego4d.revalidate_selection_evidence(chosen, task_name=TASK)
        self.assertNotIn(UNPROVEN, {row.get("clip_uid") for row in dataset})
        self.assertFalse(any(
            "Ego4D selection changed or lacks current task evidence" in str(row)
            for row in dataset))

    def test_runner_keeps_downloads_out_of_the_catalog_and_names_selection_errors(self):
        catalog = self.library
        for name in ("ego4d.json", "clips.csv", "timed_narrations.jsonl"):
            self.assertTrue((catalog / name).is_file())
        before = {path.name: path.read_bytes() for path in catalog.iterdir() if path.is_file()}

        def observe(cfg, progress=None, should_stop=None):
            marker = Path(cfg.work_dir) / "prepared.marker"
            marker.write_text("prepared", encoding="utf-8")
            self.assertFalse(str(marker.resolve()).startswith(str(catalog.resolve())))
            log = campaign.CampaignLog(started_at="fixture", accounts=["fixture@example.invalid"])
            log.status = "done"
            if progress:
                progress("campaign_done", {"status": "done", "log_path": None})
            return log

        for _ in range(2):
            runner = CampaignRunner()
            cfg = CampaignConfig(
                accounts=[AccountSpec("fixture@example.invalid", "org")],
                tasks=[TaskSpec("fold", "Cleaning / laundry", 60, 1800, task_name=TASK)],
                work_dir=catalog,
                cleanup_after_upload=True,
                dataset_provider="ego4d",
                content_mode="dataset",
            )
            with patch.object(campaign, "run_campaign", side_effect=observe):
                runner.start(cfg)
                runner._thread.join(10)
            self.assertFalse(runner._thread.is_alive())
            self.assertFalse(list(catalog.glob("prepared.marker")))
            self.assertEqual(
                {path.name: path.read_bytes() for path in catalog.iterdir() if path.is_file()},
                before)
        message = friendly_campaign_error(
            "ValueError: Ego4D selection changed or lacks current task evidence")
        self.assertNotIn("Valide a conta", message)
        self.assertTrue("seleção" in message.casefold() or "catálogo" in message.casefold())
        account = friendly_campaign_error("AuthError: http 401")
        self.assertIn("acesso", account.casefold())
