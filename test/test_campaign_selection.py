from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from moneymin import campaign
from moneymin.campaign_types import AccountSpec, CampaignConfig, TaskSpec
from moneymin.web.runner import CampaignRunner, _public_event
from moneymin.web.server import _campaign_log_view


class CampaignSelectionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(campaign.config, "DATA_DIR", self.tmp))
        self.stack.enter_context(patch.object(campaign, "_rank_cache_stamp", return_value=()))
        self.stack.enter_context(patch.object(campaign.ego4d, "has_timed_narrations", return_value=True))
        self.stack.enter_context(patch.object(campaign.ego4d, "rank_all_task_spans", return_value={}))
        self.stack.enter_context(patch.object(campaign, "_task_candidates", return_value=()))
        self.stack.enter_context(patch.object(campaign.sent_registry, "is_sent_to_all", return_value=False))
        self.stack.enter_context(patch.object(campaign.sent_registry, "sent_emails", return_value=set()))
        campaign._ranked_pools_cached.cache_clear()
        campaign._duration_ranked_pools.cache_clear()
        self.addCleanup(campaign._ranked_pools_cached.cache_clear)
        self.addCleanup(campaign._duration_ranked_pools.cache_clear)
        self.tasks = [TaskSpec("dog", "Walking the dog / pet", 180, 780,
                               task_name="Walk the Dog", count=1),
                      TaskSpec("furniture", "assembling furniture", 180, 780,
                               task_name="Furniture Assembly", count=1)]
        self.cfg = CampaignConfig(
            accounts=[AccountSpec(f"user{i}@example.com", "org") for i in range(24)],
            tasks=self.tasks, work_dir=self.tmp / "media", dataset_provider="ego4d",
            shuffle_schedule=False)
        self.seed = {
            task.task_name: tuple({"clip_uid": f"{task.task_id}-{duration}",
                                   "parent_video_uid": f"parent-{task.task_id}",
                                   "source": "ego4d", "dur_s": duration}
                                  for duration in (120, 300, 900))
            for task in self.tasks
        }
        self.stack.enter_context(patch.object(campaign, "_load_rank_seed", return_value=self.seed))
        self.stack.enter_context(redirect_stdout(StringIO()))

    def run_with_runner(self):
        runner = CampaignRunner()
        runner.state = "running"
        result = campaign.run_campaign(self.cfg, progress=runner._on_event)
        return result, runner.snapshot()

    def test_campaign_and_screen_use_same_seed_with_partial_local_catalog(self):
        session = Mock(_live=True)
        session.all_tasks.return_value = [{"id": t.task_id, "name": t.task_name}
                                         for t in self.tasks]
        visible = campaign.available_tasks("user@example.com", "org", session=session,
                                           min_dur_s=180, max_dur_s=780,
                                           dataset_provider="ego4d")
        self.assertEqual([row["clip_count"] for row in visible], [1, 1])
        events = []
        with patch.object(campaign, "_ego_clip_inputs", return_value=({}, {})), \
             patch.object(campaign, "prepare_clip", side_effect=RuntimeError("test preparation failure")), \
             patch.object(campaign, "upload_to_account") as upload:
            result = campaign.run_campaign(self.cfg, progress=lambda k, p: events.append((k, p)))
        selected = [p["clip_uid"] for k, p in events if k == "clip_prepare_start"]
        self.assertEqual(selected, ["dog-300", "furniture-300"])
        upload.assert_not_called()
        self.assertEqual(len(result.issues), 2)
        self.assertEqual(result.status, "error")

    def test_empty_selection_is_visible_persisted_and_never_success(self):
        self.seed.clear()
        result, snap = self.run_with_runner()
        saved = json.loads(result._path.read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "error")
        self.assertEqual([i["kind"] for i in saved["issues"]], ["task_empty", "task_empty"])
        self.assertEqual(snap["state"], "error")
        self.assertEqual(snap["totals"]["ok_sends"], 0)
        self.assertEqual(snap["events"][-1]["title"], "Campanha encerrada sem envios")
        self.assertEqual(snap["events"][-1]["level"], "error")
        view = _campaign_log_view(saved)
        self.assertEqual(len(view["issues"]), 2)
        self.assertIn("3m00s", view["issues"][0]["detail"])
        self.assertIn("13m00s", view["issues"][0]["detail"])

    def test_selection_exception_keeps_cause_in_history(self):
        with patch.object(campaign, "_compatible_task_clips", side_effect=RuntimeError("AccessDenied")):
            result, snap = self.run_with_runner()
        self.assertEqual(snap["state"], "error")
        self.assertEqual(result.issues[0]["error"], "RuntimeError: AccessDenied")
        self.assertEqual(len(_campaign_log_view(result.to_dict())["issues"]), 2)

    def test_disabled_account_is_excluded_while_queue_and_next_task_continue(self):
        self.cfg.accounts = self.cfg.accounts[:3]
        self.cfg.account_workers = 1
        self.cfg.account_gap_s = 0
        self.cfg.account_max_attempts = 3
        self.cfg.require_all_accounts = True
        self.cfg.cleanup_after_upload = False
        events, calls = [], []
        def send(item, account, *args, **kwargs):
            calls.append(account.email)
            if account.email == self.cfg.accounts[0].email:
                return {"email": account.email, "ok": False,
                        "error": "complete falhou após 1 tentativas: PATCH /complete falhou (403): User account is disabled. [bloqueio: user]"}
            return {"email": account.email, "ok": True}
        with patch.object(campaign, "_ego_clip_inputs", return_value=({}, {})), \
             patch.object(campaign, "prepare_clip", side_effect=lambda *a, **k: {
                 "duration_ms": 300000, "imu_real": True, "video_path": str(self.tmp / "fake.mp4")}), \
             patch.object(campaign, "upload_to_account", side_effect=send), \
             patch.object(campaign.sent_registry, "mark_sent"), \
             patch.object(campaign, "_enforce_account_video_cache", return_value=(0, 0)):
            result = campaign.run_campaign(self.cfg, progress=lambda k, p: events.append((k, p)))
        self.assertEqual(calls.count(self.cfg.accounts[0].email), 1)
        for account in self.cfg.accounts[1:]:
            self.assertEqual(calls.count(account.email), 2)
        self.assertEqual(result.status, "partial")
        self.assertEqual(sum(k == "account_excluded" for k, p in events), 1)
        self.assertFalse(any(k == "campaign_stopped" for k, p in events))
        self.assertEqual(events[-1][0], "campaign_done")

    def test_generic_disabled_word_or_403_is_not_confirmed_restriction(self):
        for error in ["HTTP 403 Forbidden", "feature disabled", "timeout: disabled proxy",
                      "PATCH /complete falhou (503): User account is disabled."]:
            self.assertFalse(campaign._is_disabled_error(error))

    def test_stop_does_not_emit_done(self):
        events = []
        result = campaign.run_campaign(self.cfg, should_stop=lambda: True,
                                        progress=lambda k, p: events.append(k))
        self.assertEqual(result.status, "stopped")
        self.assertIn("campaign_stopped", events)
        self.assertNotIn("campaign_done", events)

    def test_default_range_also_preserves_seed(self):
        self.assertEqual(len(campaign._compatible_task_clips("Walk the Dog", "ego4d")), 3)

    def test_holoassist_provider_does_not_consult_ego4d(self):
        with patch.object(campaign, "_ranked_pools", side_effect=AssertionError("wrong provider")), \
             patch.object(campaign.holoassist, "list_clips", return_value=[]) as holo:
            self.assertEqual(campaign._compatible_task_clips(
                "Furniture Assembly", "holoassist", min_dur_s=180, max_dur_s=780), ())
        holo.assert_called_once_with("Furniture Assembly", min_dur_s=180, max_dur_s=780)

    def test_terminal_events_distinguish_success_partial_and_skips(self):
        for status, successful, skipped, level in (
                ("done", 48, 0, "success"), ("partial", 24, 0, "warning"),
                ("done", 0, 48, "warning")):
            with self.subTest(status=status, successful=successful):
                event = _public_event("campaign_done", {
                    "status": status, "ok_sends": successful, "skipped_sends": skipped})
                self.assertEqual(event["level"], level)


if __name__ == "__main__":
    unittest.main()
