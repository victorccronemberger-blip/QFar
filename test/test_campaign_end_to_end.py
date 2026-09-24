"""API local -> runner real -> motor -> histórico, com provedores simulados."""
import json
import tempfile
import threading
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from moneymin import campaign, minute_api
from moneymin.campaign_types import AccountSpec, CampaignConfig, CampaignLog, TaskSpec
from moneymin.web import runner, server


class CampaignEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.instance = runner.CampaignRunner()
        self.stack.enter_context(patch.object(server, "RUNNER", self.instance))
        self.stack.enter_context(patch.object(server, "HOLO_CACHE_RUNNER", Mock(running=False)))
        self.stack.enter_context(patch.object(campaign.config, "DATA_DIR", self.root))
        self.stack.enter_context(patch.object(campaign.config, "MEDIA_DATA_DIR", self.root))
        self.emails = ["a@example.com", "b@example.com"]
        self.stack.enter_context(patch.object(server, "_list_accounts", return_value=[{"email": e} for e in self.emails]))
        self.stack.enter_context(patch.object(server, "_resolve_org", return_value="org"))
        self.stack.enter_context(patch.object(server.readiness, "campaign_readiness", return_value={"ready": True, "checks": []}))
        catalog = [{"id": "task", "name": "Furniture Assembly", "scenario": "assembling furniture", "clip_count": 1}]
        self.stack.enter_context(patch.object(campaign, "available_tasks", return_value=catalog))
        session = Mock()
        session.all_tasks.return_value = catalog
        self.stack.enter_context(patch.object(server.Session, "from_email", return_value=session))
        self.stack.enter_context(patch.object(campaign, "_compatible_task_clips", return_value=[{
            "clip_uid": "clip", "dur_s": 300, "source": "ego4d"}]))
        self.stack.enter_context(patch.object(campaign, "_ego_clip_inputs", return_value=({}, {})))
        self.prepare = self.stack.enter_context(patch.object(campaign, "prepare_clip", side_effect=lambda *a, **k: {
            "duration_ms": 300000, "video_path": str(self.root / "fixture.mp4"), "imu_real": True}))
        self.stack.enter_context(patch.object(campaign.sent_registry, "sent_emails", return_value=set()))
        self.stack.enter_context(patch.object(campaign.sent_registry, "is_sent_to_all", return_value=False))
        self.mark = self.stack.enter_context(patch.object(campaign.sent_registry, "mark_sent"))
        self.cleanup = self.stack.enter_context(patch.object(campaign, "_cleanup_uploaded_item", return_value={
            "files": 0, "bytes": 0, "errors": [], "protected": 0}))
        self.send = self.stack.enter_context(patch.object(campaign, "upload_to_account", side_effect=lambda item, account, *a, **k: {
            "email": account.email, "ok": True, "finalized": True}))
        # Sem espera de captura, perfil, download ou tráfego real neste teste de orquestração.
        self.stack.enter_context(patch.object(runner, "run_campaign", side_effect=lambda cfg, **kw: campaign.run_campaign(
            replace(cfg, realistic_timeline=False, allow_new_accounts=True, shuffle_schedule=False,
                    account_workers=1, account_retry_s=0), **kw)))
        self.stack.enter_context(patch.object(minute_api, "_request", side_effect=AssertionError("network forbidden")))
        self.client = server.create_app().test_client()
        self.body = {"accounts": self.emails, "tasks": [{"task_id": "task"}], "dataset": "ego4d"}

    def finish(self):
        self.instance._thread.join(5)
        self.assertFalse(self.instance._thread.is_alive(), "campanha não encerrou")
        snap = self.client.get("/api/campaigns/current").get_json()
        logs = list(self.root.glob("campaign_*.json"))
        self.assertEqual(len(logs), 1)
        return snap, json.loads(logs[0].read_text(encoding="utf-8"))

    def test_success_matches_polling_and_persisted_history(self):
        response = self.client.post("/api/campaigns", json=self.body)
        self.assertEqual(response.status_code, 200, response.get_json())
        snap, log = self.finish()
        self.assertEqual((snap["state"], log["status"]), ("done", "done"))
        self.assertEqual(snap["totals"]["ok_sends"], 2)
        self.assertEqual(len(log["items"][0]["accounts"]), 2)
        self.assertEqual(self.mark.call_count, 2)
        self.cleanup.assert_called_once()

    def test_task_preview_and_preflight_receive_content_mode(self):
        with patch.object(campaign, "available_tasks", return_value=[{
                "id": "task", "name": "Furniture Assembly",
                "scenario": "assembling furniture", "clip_count": 1,
                "available_for_duration": True}]) as available:
            preview = self.client.get(
                "/api/tasks?email=a%40example.com&dataset=ego4d&content_mode=cache")
            self.assertEqual(preview.status_code, 200, preview.get_json())
            self.assertEqual(available.call_args.kwargs["content_mode"], "cache")
            preflight = self.client.post(
                "/api/campaigns/preflight", json={**self.body, "content_mode": "cache"})
            self.assertEqual(preflight.status_code, 200, preflight.get_json())
            self.assertEqual(available.call_args.kwargs["content_mode"], "cache")

    def test_unknown_content_mode_is_rejected(self):
        response = self.client.post(
            "/api/campaigns", json={**self.body, "content_mode": "unknown"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("modo de conteúdo inválido", response.get_json()["error"])

    def test_prepared_cache_survives_successful_campaign(self):
        with patch("moneymin.ego_accelerator.configured_budget_gb", return_value=400), \
             patch("moneymin.ego_accelerator.ready_scenario_clips", return_value=[]), \
             patch.object(campaign, "_clip_is_cached", return_value=True), \
             patch.object(campaign, "_enforce_account_video_cache", return_value=(0, 0)):
            response = self.client.post("/api/campaigns", json=self.body)
            self.assertEqual(response.status_code, 200, response.get_json())
            snap, log = self.finish()
        self.assertEqual((snap["state"], log["status"]), ("done", "done"))
        self.cleanup.assert_not_called()
        self.assertEqual(self.prepare.call_count, 1)

    def test_prepared_holo_cache_survives_without_ego_budget(self):
        holo = {"clip_uid": "holoassist:clip", "video_name": "clip",
                "dur_s": 300, "source": "holoassist"}
        with patch("moneymin.ego_accelerator.configured_budget_gb", return_value=0), \
             patch.object(campaign.holoassist, "list_clips", return_value=[holo]), \
             patch.object(campaign, "_clip_is_cached", return_value=True), \
             patch.object(campaign, "prepare_holoassist_clip", return_value={
                 "duration_ms": 300000, "video_path": str(self.root / "holo.mp4"),
                 "imu_real": True}), \
             patch.object(campaign, "_enforce_account_video_cache", return_value=(0, 0)):
            response = self.client.post("/api/campaigns", json={**self.body, "dataset": "holoassist"})
            self.assertEqual(response.status_code, 200, response.get_json())
            snap, log = self.finish()
        self.assertEqual((snap["state"], log["status"]), ("done", "done"))
        self.cleanup.assert_not_called()
        self.prepare.assert_not_called()

    def test_mixed_cache_and_dataset_uses_cache_first_and_cleans_only_download(self):
        candidates = [
            {"clip_uid": "remote", "parent_video_uid": "remote-parent",
             "dur_s": 300, "source": "ego4d"},
            {"clip_uid": "local", "parent_video_uid": "local-parent",
             "dur_s": 300, "source": "ego4d"},
        ]
        self.prepare.side_effect = lambda row, *_args, **_kwargs: {
            "clip_uid": row["clip_uid"], "duration_ms": 300000,
            "video_path": str(self.root / "fixture.mp4"), "imu_real": True}
        with patch.object(campaign, "_compatible_task_clips", return_value=candidates), \
             patch.object(campaign, "_ego_clip_inputs", side_effect=lambda clip: (clip, {})), \
             patch.object(campaign.holoassist, "list_clips", return_value=[]), \
             patch("moneymin.ego_accelerator.configured_budget_gb", return_value=400), \
             patch("moneymin.ego_accelerator.ready_scenario_clips", return_value=[]), \
             patch.object(campaign, "_clip_is_cached", side_effect=lambda clip, _: clip["clip_uid"] == "local"), \
             patch.object(campaign, "_enforce_account_video_cache", return_value=(0, 0)):
            response = self.client.post("/api/campaigns", json={**self.body, "dataset": "all", "count": 2})
            self.assertEqual(response.status_code, 200, response.get_json())
            snap, log = self.finish()
        self.assertEqual((snap["state"], log["status"]), ("done", "done"))
        self.assertEqual([item["clip_uid"] for item in log["items"]], ["local", "remote"])
        self.cleanup.assert_called_once()

    def test_cache_only_never_selects_remote_clip(self):
        candidates = [
            {"clip_uid": "remote", "parent_video_uid": "first", "dur_s": 300, "source": "ego4d"},
            {"clip_uid": "local", "parent_video_uid": "second", "dur_s": 300, "source": "ego4d"},
        ]
        self.prepare.side_effect = lambda row, *_args, **_kwargs: {
            "clip_uid": row["clip_uid"], "duration_ms": 300000,
            "video_path": str(self.root / "fixture.mp4"), "imu_real": True}
        with patch.object(campaign, "_compatible_task_clips", return_value=candidates), \
             patch.object(campaign, "_ego_clip_inputs", side_effect=lambda clip: (clip, {})), \
             patch("moneymin.ego_accelerator.ready_scenario_clips", return_value=[]), \
             patch.object(campaign, "_clip_is_cached", side_effect=lambda clip, _: clip["clip_uid"] == "local"), \
             patch.object(campaign, "_enforce_account_video_cache", return_value=(0, 0)):
            response = self.client.post("/api/campaigns", json={**self.body, "content_mode": "cache"})
            self.assertEqual(response.status_code, 200, response.get_json())
            snap, log = self.finish()
        self.assertEqual((snap["state"], log["status"]), ("done", "done"))
        self.assertEqual([item["clip_uid"] for item in log["items"]], ["local"])
        self.cleanup.assert_not_called()
        self.assertFalse(self.prepare.call_args.kwargs["allow_download"])

    def test_cache_only_does_not_prefetch_next_clip(self):
        candidates = [
            {"clip_uid": "one", "parent_video_uid": "first", "dur_s": 300, "source": "ego4d"},
            {"clip_uid": "two", "parent_video_uid": "second", "dur_s": 300, "source": "ego4d"},
        ]
        self.prepare.side_effect = lambda row, *_args, **_kwargs: {
            "clip_uid": row["clip_uid"], "duration_ms": 300000,
            "video_path": str(self.root / "fixture.mp4"), "imu_real": True}
        with patch.object(campaign, "_compatible_task_clips", return_value=candidates), \
             patch.object(campaign, "_ego_clip_inputs", side_effect=lambda clip: (clip, {})), \
             patch("moneymin.ego_accelerator.ready_scenario_clips", return_value=[]), \
             patch.object(campaign, "_clip_is_cached", return_value=True), \
             patch.object(campaign, "_prefetch_following") as prefetch, \
             patch.object(campaign, "_enforce_account_video_cache", return_value=(0, 0)):
            response = self.client.post("/api/campaigns", json={
                **self.body, "content_mode": "cache", "count": 2})
            self.assertEqual(response.status_code, 200, response.get_json())
            snap, log = self.finish()
        self.assertEqual((snap["state"], log["status"]), ("done", "done"))
        self.assertEqual(len(log["items"]), 2)
        prefetch.assert_not_called()

    def test_dataset_mode_keeps_catalog_order_without_cache_priority(self):
        candidates = [
            {"clip_uid": "remote", "parent_video_uid": "first", "dur_s": 300, "source": "ego4d"},
            {"clip_uid": "local", "parent_video_uid": "second", "dur_s": 300, "source": "ego4d"},
        ]
        self.prepare.side_effect = lambda row, *_args, **_kwargs: {
            "clip_uid": row["clip_uid"], "duration_ms": 300000,
            "video_path": str(self.root / "fixture.mp4"), "imu_real": True}
        with patch.object(campaign, "_compatible_task_clips", return_value=candidates), \
             patch.object(campaign, "_ego_clip_inputs", side_effect=lambda clip: (clip, {})), \
             patch("moneymin.ego_accelerator.configured_budget_gb", return_value=400), \
             patch.object(campaign, "_clip_is_cached", side_effect=lambda clip, _: clip["clip_uid"] == "local"):
            response = self.client.post("/api/campaigns", json={**self.body, "content_mode": "dataset"})
            self.assertEqual(response.status_code, 200, response.get_json())
            snap, log = self.finish()
        self.assertEqual((snap["state"], log["status"]), ("done", "done"))
        self.assertEqual([item["clip_uid"] for item in log["items"]], ["remote"])
        self.cleanup.assert_called_once()

    def test_invalid_dataset_imu_falls_back_to_next_clip(self):
        candidates = [
            {"clip_uid": "invalid", "parent_video_uid": "first",
             "dur_s": 300, "source": "ego4d"},
            {"clip_uid": "valid", "parent_video_uid": "second",
             "dur_s": 300, "source": "ego4d"},
        ]
        def prepare(row, *_args, **_kwargs):
            if row["clip_uid"] == "invalid":
                raise RuntimeError("cobertura IMU insuficiente")
            return {"clip_uid": row["clip_uid"], "duration_ms": 300000,
                    "video_path": str(self.root / "fixture.mp4"), "imu_real": True}
        self.prepare.side_effect = prepare
        with patch.object(campaign, "_compatible_task_clips", return_value=candidates), \
             patch.object(campaign, "_ego_clip_inputs", side_effect=lambda clip: (clip, {})):
            response = self.client.post("/api/campaigns", json={**self.body, "count": 1})
            self.assertEqual(response.status_code, 200, response.get_json())
            snap, log = self.finish()
        self.assertEqual((snap["state"], log["status"]), ("done", "partial"))
        self.assertTrue(any(issue["kind"] == "clip_prepare_done" for issue in log["issues"]))
        self.assertEqual([item["clip_uid"] for item in log["items"]], ["valid"])
        self.assertEqual(self.prepare.call_count, 2)
        self.assertEqual(self.send.call_count, 2)

    def test_catalog_shortfall_is_partial_not_completed(self):
        response = self.client.post("/api/campaigns", json={**self.body, "count": 3})
        self.assertEqual(response.status_code, 200)
        snap, log = self.finish()
        self.assertEqual(log["status"], "partial")
        self.assertTrue(any(i["kind"] == "task_shortfall" for i in log["issues"]))
        self.assertEqual(snap["totals"]["ok_sends"], 2)
        public = server._campaign_log_view(log)
        self.assertTrue(any(i["title"] == "Meta não atingida" for i in public["issues"]))

    def test_unreached_hours_goal_is_partial_not_completed(self):
        response = self.client.post("/api/campaigns", json={**self.body, "target_hours": 1})
        self.assertEqual(response.status_code, 200)
        snap, log = self.finish()
        self.assertEqual(log["status"], "partial")
        issue = next(i for i in log["issues"] if i["kind"] == "goal_shortfall")
        self.assertEqual(issue["remaining_seconds"], {e: 3300 for e in self.emails})

    def test_transient_failure_before_session_creation_retries_once(self):
        attempts = {}
        def send(item, account, *args, **kwargs):
            attempts[account.email] = attempts.get(account.email, 0) + 1
            if attempts[account.email] == 1:
                return {"email": account.email, "ok": False, "retryable": True, "error": "timeout"}
            return {"email": account.email, "ok": True, "finalized": True}
        self.send.side_effect = send
        with patch.object(runner, "run_campaign", side_effect=lambda cfg, **kw: campaign.run_campaign(
                replace(cfg, realistic_timeline=False, allow_new_accounts=True, account_workers=1, account_max_attempts=2,
                        account_retry_s=0, shuffle_schedule=False), **kw)):
            self.client.post("/api/campaigns", json=self.body)
            snap, log = self.finish()
        self.assertEqual(log["status"], "done")
        self.assertEqual(attempts, {e: 2 for e in self.emails})
        self.assertEqual(snap["totals"]["ok_sends"], 2)
        self.assertEqual(self.mark.call_count, 2)

    def test_exhausted_catalog_does_not_erase_history_or_reupload(self):
        with patch.object(campaign.sent_registry, "is_sent_to_all", return_value=True), \
             patch.object(campaign.sent_registry, "reset") as reset:
            self.client.post("/api/campaigns", json=self.body)
            snap, log = self.finish()
        reset.assert_not_called()
        self.prepare.assert_not_called()
        self.send.assert_not_called()
        self.assertEqual(log["issues"][0]["kind"], "task_exhausted")
        self.assertEqual(snap["state"], "error")

    def test_campaign_alternates_uncached_parent_videos(self):
        candidates = [{"clip_uid": uid, "parent_video_uid": parent, "dur_s": 300, "source": "ego4d"}
                      for uid, parent in (("a1", "a"), ("a2", "a"), ("b1", "b"))]
        with patch.object(campaign, "_compatible_task_clips", return_value=candidates), \
             patch.object(campaign, "_ego_clip_inputs", return_value=({}, {})) as inputs:
            response = self.client.post("/api/campaigns", json={**self.body, "accounts": self.emails[:1], "count": 3})
            self.assertEqual(response.status_code, 200)
            snap, log = self.finish()
        self.assertEqual([call.args[0]["clip_uid"] for call in inputs.call_args_list], ["a1", "b1", "a2"])
        self.assertEqual(snap["totals"]["ok_sends"], 3)

    def test_completed_account_is_persisted_before_next_account_finishes(self):
        entered, release = threading.Event(), threading.Event()
        def send(item, account, *args, **kwargs):
            if account.email == self.emails[1]:
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test release timeout")
            return {"email": account.email, "ok": True}
        self.send.side_effect = send
        self.client.post("/api/campaigns", json=self.body)
        try:
            self.assertTrue(entered.wait(5))
            log = json.loads(next(self.root.glob("campaign_*.json")).read_text(encoding="utf-8"))
            self.assertEqual(len(log["items"]), 1)
            self.assertEqual([a["email"] for a in log["items"][0]["accounts"]], self.emails[:1])
        finally:
            release.set()
            self.instance._thread.join(5)
        _, log = self.finish()
        self.assertEqual(len(log["items"]), 1)
        self.assertEqual(len(log["items"][0]["accounts"]), 2)

    def test_registry_write_failure_preserves_success_history_and_stops_new_sends(self):
        self.mark.side_effect = OSError("disk unavailable")
        self.client.post("/api/campaigns", json=self.body)
        snap, log = self.finish()
        self.assertEqual((snap["state"], log["status"]), ("error", "error"))
        self.assertTrue(log["items"][0]["accounts"][0]["ok"])
        self.assertEqual(snap["totals"]["ok_sends"], 1)
        self.assertEqual(self.send.call_count, 1)
        self.cleanup.assert_not_called()

    def test_persistence_failure_drains_parallel_workers_and_keeps_their_results(self):
        barrier = threading.Barrier(2)
        def send(item, account, *args, **kwargs):
            barrier.wait(5)
            return {"email": account.email, "ok": True}
        self.send.side_effect = send
        self.mark.side_effect = [OSError("disk unavailable"), None]
        with patch.object(runner, "run_campaign", side_effect=lambda cfg, **kw: campaign.run_campaign(
                replace(cfg, realistic_timeline=False, allow_new_accounts=True, shuffle_schedule=False,
                        account_workers=2, account_gap_s=0, account_retry_s=0), **kw)):
            self.client.post("/api/campaigns", json=self.body)
            snap, log = self.finish()
        self.assertEqual((snap["state"], log["status"]), ("error", "error"))
        self.assertEqual({row["email"] for row in log["items"][0]["accounts"]}, set(self.emails))
        self.assertEqual(snap["totals"]["ok_sends"], 2)
        self.assertEqual(self.send.call_count, 2)
        self.cleanup.assert_not_called()

    def test_stop_during_prepare_prevents_every_send(self):
        entered, release = threading.Event(), threading.Event()
        def prepare(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test release timeout")
            return {"duration_ms": 300000, "video_path": str(self.root / "fixture.mp4"), "imu_real": True}
        self.prepare.side_effect = prepare
        self.client.post("/api/campaigns", json=self.body)
        try:
            self.assertTrue(entered.wait(5))
            self.client.post("/api/campaigns/stop")
        finally:
            release.set()
            self.instance._thread.join(5)
        snap, log = self.finish()
        self.assertEqual((snap["state"], log["status"]), ("stopped", "stopped"))
        self.send.assert_not_called()
        self.cleanup.assert_not_called()

    def test_explicit_readiness_failure_blocks_start(self):
        with patch.object(server.readiness, "campaign_readiness", return_value={"ready": False, "checks": []}):
            response = self.client.post("/api/campaigns", json=self.body)
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.instance._thread)

    def test_malformed_catalog_is_handled_by_preflight_and_start(self):
        for catalog in (None, {}, [None]):
            for endpoint in ("/api/campaigns/preflight", "/api/campaigns"):
                with self.subTest(catalog=catalog, endpoint=endpoint), patch.object(
                        campaign, "available_tasks", return_value=catalog):
                    response = self.client.post(endpoint, json=self.body)
                    self.assertIn(response.status_code, (200, 400))
                    self.assertFalse(response.get_json().get("ok", False))
        self.assertIsNone(self.instance._thread)

    def test_permanent_failure_is_not_retried_and_preserves_media(self):
        self.send.side_effect = lambda item, account, *a, **k: {
            "email": account.email, "ok": account.email == self.emails[0],
            "error": "policy refused", "retryable": False}
        self.client.post("/api/campaigns", json=self.body)
        snap, log = self.finish()
        self.assertEqual(log["status"], "partial")
        self.assertEqual(snap["totals"]["failed_sends"], 1)
        self.assertEqual(self.send.call_count, 2)
        self.cleanup.assert_not_called()

    def test_uncertain_existing_session_never_restarts_whole_upload(self):
        self.send.side_effect = lambda item, account, *a, **k: {
            "email": account.email, "ok": False, "error": "timeout",
            "retryable": True, "session_id": "persisted-session"}
        self.client.post("/api/campaigns", json=self.body)
        snap, log = self.finish()
        self.assertEqual(log["status"], "error")
        self.assertEqual(self.send.call_count, 2)
        self.cleanup.assert_not_called()
        self.mark.assert_not_called()

    def test_prepare_failure_never_uploads_or_reports_success(self):
        self.prepare.side_effect = RuntimeError("fixture failed")
        self.client.post("/api/campaigns", json=self.body)
        snap, log = self.finish()
        self.assertEqual((snap["state"], log["status"]), ("error", "error"))
        self.send.assert_not_called()
        self.cleanup.assert_not_called()

    def test_stop_drains_current_send_and_prevents_next_account(self):
        entered, release = threading.Event(), threading.Event()
        def send(item, account, *args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test release timeout")
            return {"email": account.email, "ok": True}
        self.send.side_effect = send
        self.client.post("/api/campaigns", json=self.body)
        try:
            self.assertTrue(entered.wait(5))
            response = self.client.post("/api/campaigns/stop").get_json()
            self.assertEqual(response["state"], "stopping")
        finally:
            release.set()
        snap, log = self.finish()
        self.assertEqual((snap["state"], log["status"]), ("stopped", "stopped"))
        self.assertTrue(snap["log_path"])
        self.assertEqual(self.send.call_count, 1)
        self.cleanup.assert_not_called()
        self.assertFalse(any(e["kind"] == "campaign_done" for e in snap["events"]))

    def test_invalid_input_is_rejected_before_start_or_provider_access(self):
        for path in ("/api/campaigns", "/api/campaigns/preflight"):
            for change in ({"accounts": "a@example.com"}, {"accounts": None},
                           {"accounts": ["a@example.com", " A@EXAMPLE.COM "]},
                           {"tasks": {}}, {"tasks": [None]},
                           {"target_hours": "nan"}, {"target_hours": "inf"}):
                with self.subTest(path=path, change=change):
                    response = self.client.post(path, json={**self.body, **change})
                    self.assertEqual(response.status_code, 400, response.get_json())
        self.assertIsNone(self.instance._thread)
        self.prepare.assert_not_called()
        self.send.assert_not_called()

    def test_secondary_account_auth_failure_blocks_entire_start(self):
        valid = Mock()
        valid.all_tasks.return_value = [{"id": "task"}]
        def session_for(email, **kwargs):
            if email == self.emails[1]:
                raise minute_api.AuthError("serviço indisponível", code="service")
            return valid
        with patch.object(server.Session, "from_email", side_effect=session_for):
            response = self.client.post("/api/campaigns", json=self.body)
        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertIsNone(self.instance._thread)
        self.prepare.assert_not_called()
        self.send.assert_not_called()

    def test_invalid_secondary_catalog_blocks_start_without_server_error(self):
        for catalog in (None, {}, [None]):
            valid, invalid = Mock(), Mock()
            valid.all_tasks.return_value = [{"id": "task"}]
            invalid.all_tasks.return_value = catalog
            with self.subTest(catalog=catalog), patch.object(
                    server.Session, "from_email", side_effect=lambda email, **kw:
                    valid if email == self.emails[0] else invalid):
                response = self.client.post("/api/campaigns", json=self.body)
                self.assertEqual(response.status_code, 400, response.get_json())
        self.assertIsNone(self.instance._thread)
        self.send.assert_not_called()


class CampaignPersistenceTests(unittest.TestCase):
    def test_invalid_config_does_not_strand_runner_in_running_state(self):
        for hours in (float("inf"), float("nan"), -1, "invalid", 1e308):
            instance = runner.CampaignRunner()
            with self.subTest(hours=hours), self.assertRaises(RuntimeError):
                instance.start(CampaignConfig([], [], target_hours_per_account=hours))
            self.assertFalse(instance.running)
            self.assertIsNone(instance._thread)

    def test_returned_error_log_without_terminal_event_is_not_success(self):
        instance = runner.CampaignRunner()
        with patch.object(runner, "run_campaign", return_value=CampaignLog("now", [], status="error")):
            instance.start(CampaignConfig([], []))
            instance._thread.join(5)
        self.assertEqual(instance.state, "error")

    def test_campaigns_started_in_same_second_have_distinct_logs(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(campaign.config, "DATA_DIR", Path(tmp)), \
             patch("moneymin.campaign_types.time.strftime", return_value="same_second"):
            first, second = CampaignLog("now", []), CampaignLog("now", [])
            self.assertNotEqual(first.save(), second.save())
            original = first._path
            self.assertEqual(first.save(), original)

    def test_unexpected_failure_is_persisted_and_releases_prefetch(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(campaign.config, "DATA_DIR", Path(tmp)), \
             patch.object(campaign, "_ClipPrefetch") as prefetch, \
             patch.object(campaign, "_run_campaign", side_effect=RuntimeError("unexpected failure")):
            cfg = CampaignConfig([AccountSpec("a@example.com", "org")], [TaskSpec("task", "scenario", 60, 600)])
            with self.assertRaisesRegex(RuntimeError, "unexpected failure"):
                campaign.run_campaign(cfg)
            prefetch.return_value.shutdown.assert_called_once()
            log = json.loads(next(Path(tmp).glob("campaign_*.json")).read_text(encoding="utf-8"))
            self.assertEqual(log["status"], "error")
            self.assertEqual(log["issues"][0]["kind"], "campaign_error")

    def test_terminal_event_does_not_release_runner_before_engine_returns(self):
        instance = runner.CampaignRunner()
        entered, release = threading.Event(), threading.Event()
        def engine(cfg, progress, **kwargs):
            progress("campaign_stopped", {})
            entered.set()
            release.wait(5)
        with patch.object(runner, "run_campaign", side_effect=engine):
            instance.start(CampaignConfig([], []))
            try:
                self.assertTrue(entered.wait(5))
                self.assertTrue(instance.running)
                self.assertEqual(instance.snapshot()["state"], "running")
                with self.assertRaises(RuntimeError):
                    instance.start(CampaignConfig([], []))
            finally:
                release.set()
                instance._thread.join(5)
        self.assertEqual(instance.state, "stopped")

    def test_polling_cursor_remains_monotonic_between_campaigns(self):
        instance = runner.CampaignRunner()
        def engine(cfg, progress, **kwargs):
            progress("campaign_start", {})
            progress("campaign_done", {"status": "done", "ok_sends": 1})
        with patch.object(runner, "run_campaign", side_effect=engine):
            instance.start(CampaignConfig([], []))
            instance._thread.join(5)
            cursor = instance.snapshot()["last_seq"]
            instance.start(CampaignConfig([], []))
            instance._thread.join(5)
        self.assertTrue(instance.snapshot(since=cursor)["events"])
        self.assertGreater(instance.snapshot()["last_seq"], cursor)
