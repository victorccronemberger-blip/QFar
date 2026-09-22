import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from moneymin import campaign, ego_accelerator
from moneymin.web.runner import HoloCacheRunner


def _row() -> dict:
    return {
        "exported_clip_uid": "clip-1",
        "parent_video_uid": "parent-1",
        "parent_start_sec": "0",
        "parent_end_sec": "120",
        "s3_path": "s3://ego4d-test/clip-1.mp4",
        "needs_cut": False,
    }


def _imu(path: Path) -> None:
    header = "canonical_timestamp_ms,gyro_x,gyro_y,gyro_z,accl_x,accl_y,accl_z\n"
    path.write_text(header + ("0,0,0,0,0,0,0\n" * 20), encoding="utf-8")


class EgoCacheStateTests(unittest.TestCase):
    def test_ready_requires_source_imu_and_matching_marker(self):
        row = _row()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(campaign, "_ego_clip_inputs", return_value=(row, {"video_uid": "parent-1"})):
                self.assertEqual(campaign.ego_clip_cache_state(row, root), "pending")
                plan = campaign._ego_prepare_plan(row)
                source = root / plan["source_name"]
                source.write_bytes(b"\0" * (1024 * 1024 + 8))
                self.assertEqual(campaign.ego_clip_cache_state(row, root), "partial")
                _imu(root / plan["imu_name"])
                native = root / plan["native_name"]
                native.write_bytes(b"\0" * (1024 * 1024 + 8))
                marker = native.with_name(native.name + ".source.json")
                marker.write_text(json.dumps(
                    campaign._native_cache_key(source, plan["norm_start"], plan["dur_s"]),
                    sort_keys=True,
                ), encoding="utf-8")
                self.assertEqual(campaign.ego_clip_cache_state(row, root), "ready")
                marker.write_text('{"version": 1}', encoding="utf-8")
                self.assertEqual(campaign.ego_clip_cache_state(row, root), "partial")

    def test_missing_catalog_row_is_unresolved(self):
        with patch.object(campaign, "_ego_clip_inputs", return_value=(None, None)):
            self.assertEqual(
                campaign.ego_clip_cache_state({"clip_uid": "x"}, Path(".")),
                "unresolved",
            )


class EgoWarmTests(unittest.TestCase):
    def test_file_stop_during_catalog_prevents_prepare(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def catalogue(*args, **kwargs):
                (root / "stop").write_text("stop")
                return [{"clip_uid": "clip-1"}]

            with patch.object(ego_accelerator, "eligible_clips", side_effect=catalogue), \
                 patch.object(ego_accelerator, "stop_path", return_value=root / "stop"), \
                 patch.object(ego_accelerator, "state_path", return_value=root / "state.json"), \
                 patch.object(campaign, "prepare_clip") as prepare:
                result = ego_accelerator.warm_cache(work_dir=root)
            self.assertEqual(result["status"], "stopped")
            prepare.assert_not_called()

    def test_stop_during_catalog_prevents_prepare(self):
        stop = threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def catalogue(*args, **kwargs):
                stop.set()
                (root / "stop").write_text("stop")
                return [{"clip_uid": "clip-1"}]

            with patch.object(ego_accelerator, "eligible_clips", side_effect=catalogue), \
                 patch.object(ego_accelerator, "stop_path", return_value=root / "stop"), \
                 patch.object(ego_accelerator, "state_path", return_value=root / "state.json"), \
                 patch.object(campaign, "prepare_clip") as prepare:
                result = ego_accelerator.warm_cache(work_dir=root, should_stop=stop.is_set)
            self.assertEqual(result["status"], "stopped")
            self.assertEqual(result["provider"], "ego4d")
            prepare.assert_not_called()

    def test_ready_clip_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(ego_accelerator, "eligible_clips", return_value=[{"clip_uid": "clip-1"}]), \
                 patch.object(ego_accelerator, "stop_path", return_value=root / "stop"), \
                 patch.object(ego_accelerator, "state_path", return_value=root / "state.json"), \
                 patch.object(campaign, "ego_clip_cache_state", return_value="ready"), \
                 patch.object(campaign, "prepare_clip") as prepare:
                result = ego_accelerator.warm_cache(work_dir=root)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["skipped"], 1)
            self.assertEqual(result["ready"], 1)
            prepare.assert_not_called()

    def test_disk_limit_stops_before_prepare(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            class Usage:
                free = 1024

            with patch.object(ego_accelerator, "eligible_clips", return_value=[{"clip_uid": "clip-1"}]), \
                 patch.object(ego_accelerator, "stop_path", return_value=root / "stop"), \
                 patch.object(ego_accelerator, "state_path", return_value=root / "state.json"), \
                 patch.object(campaign, "ego_clip_cache_state", return_value="pending"), \
                 patch.object(ego_accelerator.shutil, "disk_usage", return_value=Usage()), \
                 patch.object(campaign, "prepare_clip") as prepare:
                result = ego_accelerator.warm_cache(work_dir=root, min_free_gb=50)
            self.assertEqual(result["status"], "disk_limit")
            prepare.assert_not_called()

    def test_prepare_uses_the_campaign_inputs(self):
        row = _row()
        video = {"video_uid": "parent-1", "has_imu": True}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(ego_accelerator, "eligible_clips", return_value=[row]), \
                 patch.object(ego_accelerator, "stop_path", return_value=root / "stop"), \
                 patch.object(ego_accelerator, "state_path", return_value=root / "state.json"), \
                 patch.object(campaign, "ego_clip_cache_state", return_value="pending"), \
                 patch.object(campaign, "_ego_clip_inputs", return_value=(row, video)), \
                 patch.object(ego_accelerator.shutil, "disk_usage", return_value=type("U", (), {"free": 10 ** 15})()), \
                 patch.object(campaign, "prepare_clip", return_value={}) as prepare:
                result = ego_accelerator.warm_cache(work_dir=root, min_free_gb=1)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["ready"], 1)
            prepare.assert_called_once()
            self.assertIs(prepare.call_args.args[0], row)
            self.assertIs(prepare.call_args.args[1], video)
            self.assertEqual(prepare.call_args.args[2], root)


class EgoBudgetTests(unittest.TestCase):
    def test_plan_skips_clips_larger_than_budget(self):
        with patch.object(ego_accelerator, "task_names", return_value=[]), \
             patch.object(ego_accelerator, "eligible_clips", return_value=[
                 {"clip_uid": "large", "dur_s": 120},
                 {"clip_uid": "small", "dur_s": 120},
             ]), \
             patch.object(ego_accelerator, "scenario_buckets", return_value={}), \
             patch.object(ego_accelerator, "estimate_clip_bytes",
                          side_effect=[ego_accelerator.budget_bytes(500) + 1, 1]):
            clips = ego_accelerator.allocation_plan(budget_gb=500)
        self.assertEqual([clip["clip_uid"] for clip in clips], ["small"])

    def test_remaining_budget_is_checked_before_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(ego_accelerator, "allocation_plan", return_value=[
                     {"clip_uid": "clip-1", "dur_s": 120}]), \
                 patch.object(ego_accelerator, "remember_budget"), \
                 patch.object(ego_accelerator, "stop_path", return_value=root / "stop"), \
                 patch.object(ego_accelerator, "state_path", return_value=root / "state.json"), \
                 patch.object(ego_accelerator, "used_bytes",
                              return_value=ego_accelerator.budget_bytes(500) - 1), \
                 patch.object(campaign, "ego_clip_cache_state", return_value="pending"), \
                 patch.object(campaign, "prepare_clip") as prepare:
                result = ego_accelerator.warm_cache(work_dir=root, budget_gb=500)
            self.assertEqual(result["status"], "budget")
            prepare.assert_not_called()

    def test_non_object_budget_is_treated_as_disabled(self):
        for payload in (None, [], "invalid", 3):
            with self.subTest(payload=payload), \
                 patch.object(ego_accelerator, "load_json", return_value=payload):
                self.assertEqual(ego_accelerator.configured_budget_gb(), 0)

    def test_phone_clip_is_not_assigned_and_gardening_is(self):
        clips = [
            {"clip_uid": "garden", "dur_s": 120, "scenarios": ["Gardening"], "action_text": ""},
            {"clip_uid": "phone", "dur_s": 120, "scenarios": ["Gardening"],
             "action_text": "#C C looks at the phone"},
            {"clip_uid": "sport", "dur_s": 120, "scenarios": ["Playing basketball"],
             "action_text": ""},
        ]
        buckets = ego_accelerator.assign_scenario_clips(clips)
        self.assertEqual([clip["clip_uid"] for clip in buckets["Gardening"]], ["garden"])
        assigned = [clip["clip_uid"] for rows in buckets.values() for clip in rows]
        self.assertNotIn("phone", assigned)
        self.assertNotIn("sport", assigned)

    def test_used_bytes_ignore_holoassist_and_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "clip.mp4").write_bytes(b"a" * 1000)
            (root / "holoassist_x_native.mp4").write_bytes(b"b" * 5000)
            (root / "ego4d.json").write_bytes(b"c" * 800)
            (root / "parent_imu.csv").write_bytes(b"d" * 200)
            self.assertEqual(ego_accelerator.used_bytes(root), 1200)

    def test_budget_stops_before_any_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(ego_accelerator, "allocation_plan", return_value=[{"clip_uid": "clip-1"}]), \
                 patch.object(ego_accelerator, "remember_budget"), \
                 patch.object(ego_accelerator, "stop_path", return_value=root / "stop"), \
                 patch.object(ego_accelerator, "state_path", return_value=root / "state.json"), \
                 patch.object(ego_accelerator, "used_bytes", return_value=ego_accelerator.budget_bytes(500)), \
                 patch.object(campaign, "prepare_clip") as prepare:
                result = ego_accelerator.warm_cache(work_dir=root, budget_gb=500)
            self.assertEqual(result["status"], "budget")
            prepare.assert_not_called()

    def test_ready_scenario_clips_stay_empty_without_budget(self):
        with patch.object(ego_accelerator, "configured_budget_gb", return_value=0), \
             patch.object(ego_accelerator, "scenario_buckets", return_value={"Gardening": [{"clip_uid": "g", "dur_s": 90}]}):
            self.assertEqual(ego_accelerator.ready_scenario_clips("Gardening"), [])


class CampaignCacheOrderTests(unittest.TestCase):
    def test_holo_only_catalog_does_not_include_ego_cache(self):
        session = Mock(_live=True)
        session.all_tasks.return_value = [{"id": "garden", "name": "Gardening"}]
        with patch.object(campaign, "_compatible_task_clips", return_value=[]), \
             patch.object(ego_accelerator, "ready_scenario_clips", return_value=[{
                 "clip_uid": "ego", "source": "ego4d", "dur_s": 120,
             }]) as expansion:
            rows = campaign.available_tasks(
                "unused", "org", session=session, dataset_provider="holoassist",
                include_unavailable=True)
        expansion.assert_not_called()
        self.assertEqual(rows[0]["clip_count"], 0)
        self.assertFalse(rows[0]["available_for_duration"])

    def test_without_cache_the_provider_order_stays(self):
        clips = [
            {"clip_uid": "a", "source": "ego4d"},
            {"clip_uid": "b", "source": "ego4d"},
        ]
        with patch.object(ego_accelerator, "configured_budget_gb", return_value=0):
            ordered = campaign._prefer_cached_clips(clips, Path("."))
        self.assertEqual([clip["clip_uid"] for clip in ordered], ["a", "b"])

    def test_with_cache_ready_clips_come_before_the_provider(self):
        clips = [
            {"clip_uid": "remote", "source": "ego4d"},
            {"clip_uid": "local", "source": "ego4d"},
            {"clip_uid": "holo-remote", "source": "holoassist"},
        ]

        def cached(clip, work):
            return clip["clip_uid"] == "local"

        with patch.object(ego_accelerator, "configured_budget_gb", return_value=1), \
             patch.object(campaign, "_clip_is_cached", side_effect=cached):
            ordered = campaign._prefer_cached_clips(clips, Path("."))
        self.assertEqual(
            [clip["clip_uid"] for clip in ordered],
            ["local", "remote", "holo-remote"])

    def test_zero_budget_gb_does_not_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(ego_accelerator, "remember_budget") as remember, \
                 patch.object(campaign, "prepare_clip") as prepare:
                result = ego_accelerator.warm_cache(work_dir=root, budget_gb=0)
            self.assertEqual(result["status"], "provider")
            remember.assert_called_once_with(0)
            prepare.assert_not_called()


class EgoRunnerTests(unittest.TestCase):
    def test_runner_dispatches_ego4d(self):
        runner = HoloCacheRunner()
        with patch.object(ego_accelerator, "warm_cache", return_value={"status": "complete", "ready": 2, "failed": 0, "total": 2}) as warm:
            runner._run(provider="ego4d", task="Furniture Assembly")
        warm.assert_called_once()
        self.assertEqual(warm.call_args.kwargs["task"], "Furniture Assembly")
        self.assertNotIn("provider", warm.call_args.kwargs)
        self.assertEqual(runner.state, "done")

    def test_holo_runner_does_not_forward_budget_gb(self):
        from moneymin import holo_accelerator
        runner = HoloCacheRunner()
        with patch.object(holo_accelerator, "warm_cache", return_value={"status": "complete"}) as warm:
            runner._run(provider="holoassist", task="Furniture Assembly", budget_gb=1500)
        self.assertNotIn("budget_gb", warm.call_args.kwargs)


class EgoStorageLimitTests(unittest.TestCase):
    def limits(self, requested, *, free=300, used=100, reserve=50):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(ego_accelerator.shutil, "disk_usage",
                          return_value=Mock(free=free * 1024 ** 3)), \
             patch.object(ego_accelerator, "used_bytes", return_value=used * 1024 ** 3):
            return ego_accelerator.storage_limits(
                requested, min_free_gb=reserve, work_dir=Path(tmp) / "not-created")

    def test_caps_total_including_existing_cache_and_reserve(self):
        result = self.limits(400)
        self.assertEqual(result["budget_gb"], 350)
        self.assertEqual(result["max_budget_gb"], 350)
        self.assertEqual(result["requested_budget_gb"], 400)

    def test_accepts_custom_size_when_it_fits(self):
        self.assertEqual(self.limits(237)["budget_gb"], 237)

    def test_no_new_space_below_reserve(self):
        self.assertEqual(self.limits(400, free=20, used=0)["budget_gb"], 0)
        self.assertEqual(self.limits(400, free=20, used=100)["budget_gb"], 100)

    def test_zero_keeps_cache_disabled(self):
        result = self.limits(0)
        self.assertEqual(result["budget_gb"], 0)
        self.assertEqual(result["cache_mode"], "provider")

    def test_old_settings_migrate_from_blocks(self):
        with patch.object(ego_accelerator, "load_json", return_value={"blocks": 2}):
            self.assertEqual(ego_accelerator.configured_budget_gb(), 1000)

    def test_custom_budget_is_persisted_in_gb(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(ego_accelerator, "budget_path", return_value=Path(tmp) / "budget.json"):
            ego_accelerator.remember_budget(400)
            self.assertEqual(ego_accelerator.configured_budget_gb(), 400)

    def test_worker_rechecks_space_before_planning(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(ego_accelerator.shutil, "disk_usage", return_value=Mock(free=20 * 1024 ** 3)), \
             patch.object(ego_accelerator, "allocation_plan") as plan, \
             patch.object(campaign, "prepare_clip") as prepare:
            result = ego_accelerator.warm_cache(
                budget_gb=400, min_free_gb=50, work_dir=Path(tmp))
        self.assertEqual(result["status"], "disk_limit")
        plan.assert_not_called()
        prepare.assert_not_called()


class EgoCacheApiTests(unittest.TestCase):
    def setUp(self):
        from moneymin.web import server
        self.server = server
        self.client = server.create_app().test_client()

    def test_invalid_sizes_are_rejected(self):
        for value in (-1, 2.5, True, "nan", "inf"):
            with self.subTest(value=value):
                response = self.client.post("/api/holo-cache/start", json={
                    "provider": "ego4d", "budget_gb": value})
                self.assertEqual(response.status_code, 400)

    def test_start_forwards_disk_capped_budget(self):
        with patch.object(ego_accelerator, "cache_status", return_value={"budget_gb": 250}) as status, \
             patch.object(self.server, "HOLO_CACHE_RUNNER", Mock(running=False)) as runner, \
             patch.object(self.server, "RUNNER", Mock(running=False)):
            runner.snapshot.return_value = {}
            response = self.client.post("/api/holo-cache/start", json={
                "provider": "ego4d", "budget_gb": 400, "min_free_gb": 50})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(status.call_args.kwargs["budget_gb"], 400)
        self.assertEqual(runner.start.call_args.kwargs["budget_gb"], 250)

    def test_no_space_does_not_start_worker(self):
        with patch.object(ego_accelerator, "cache_status", return_value={"budget_gb": 0}), \
             patch.object(self.server, "HOLO_CACHE_RUNNER") as runner:
            response = self.client.post("/api/holo-cache/start", json={
                "provider": "ego4d", "budget_gb": 400})
        self.assertEqual(response.status_code, 400)
        runner.start.assert_not_called()
