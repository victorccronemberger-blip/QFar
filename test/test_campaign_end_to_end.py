"""API local -> runner real -> motor -> histórico, com provedores simulados."""
import json
import tempfile
import threading
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from moneymin import campaign, minute_api, upload
from moneymin.campaign_types import AccountSpec, CampaignConfig, CampaignLog, TaskSpec
from moneymin.web import runner, server

REAL_UPLOAD_TO_ACCOUNT = campaign.upload_to_account


class CampaignEndToEndTests(unittest.TestCase):
    def test_known_pending_clip_allows_other_content_without_resending_or_counting_it(self):
        pending = [{"email": email, "clip_uid": "clip", "blocks_campaign": False}
                   for email in self.emails]
        candidates = [{"clip_uid": uid, "dur_s": 300, "source": "ego4d"} for uid in ("clip", "fresh")]
        with patch.object(server.recovery, "snapshot", return_value={"items": pending}), \
             patch.object(campaign, "_ego_clip_inputs", side_effect=lambda clip: (clip, {})), \
             patch.object(campaign, "_compatible_task_clips", return_value=candidates):
            review = self.client.post("/api/campaigns/preflight", json={**self.body, "include_clip_plan": True}).get_json()
            reserved = next(row for row in review["clip_plan"] if row["clip_uid"] == "clip")
            self.assertEqual(reserved["eligible_accounts"], [])
            self.assertEqual(reserved["pending_accounts"], self.emails)
            response = self.client.post("/api/campaigns", json=self.body)
            self.assertEqual(response.status_code, 200, response.get_json())
            snap, log = self.finish()
        self.assertEqual([call.args[0]["clip_uid"] for call in self.prepare.call_args_list], ["fresh"])
        self.assertEqual(snap["totals"]["ok_sends"], 2)
        self.assertEqual(self.mark.call_count, 2)
        self.assertTrue(all(not call.kwargs["recover_pending"] for call in self.send.call_args_list))

    def test_pending_clip_reservation_is_per_account_and_preserves_media(self):
        pending = [{"email": self.emails[0], "clip_uid": "clip", "blocks_campaign": False}]
        self.cleanup.return_value = {"files": 0, "bytes": 0, "errors": [], "protected": 1,
                                     "retained_managed": 1}
        with patch.object(server.recovery, "snapshot", return_value={"items": pending}):
            response = self.client.post("/api/campaigns", json=self.body)
            self.assertEqual(response.status_code, 200, response.get_json())
            snap, log = self.finish()
        self.assertEqual([call.args[1].email for call in self.send.call_args_list], self.emails[1:])
        self.assertEqual(snap["totals"]["ok_sends"], 1)
        self.cleanup.assert_called_once()
        self.assertEqual(log["status"], "error")

    def test_replaced_archive_cleanup_error_preserves_confirmed_batch_and_halts_acquisition(self):
        candidates = [
            {"clip_uid": uid, "dur_s": 300, "source": "ego4d"}
            for uid in ("clip-a", "clip-b")
        ]
        prepared = []
        sent = []

        def prepare(clip, *_args, **_kwargs):
            prepared.append(clip["clip_uid"])
            return {"duration_ms": 300000,
                    "video_path": str(self.root / f"{clip['clip_uid']}.mp4"),
                    "imu_real": True}

        original_send = self.send.side_effect

        def send(item, account, *args, **kwargs):
            sent.append((item["clip_uid"], account.email))
            return original_send(item, account, *args, **kwargs)

        self.prepare.side_effect = prepare
        self.send.side_effect = send
        with patch.object(campaign, "_compatible_task_clips", return_value=candidates), \
             patch.object(campaign, "_ego_clip_inputs", side_effect=lambda clip: (clip, {})), \
             patch("moneymin.recovery.reconcile_confirmed", return_value={
                 "archive_cleanup_errors": [{"code": "terminal_archive_preserved"}],
                 "archives_removed": 0, "archive_bytes_removed": 0}) as reconcile:
            response = self.client.post("/api/campaigns", json={
                **self.body, "cleanup_after_upload": True})
            self.assertEqual(response.status_code, 200, response.get_json())
            snapshot, history = self.finish()

        self.assertEqual(history["status"], "error")
        self.assertEqual(prepared, ["clip-a"])
        self.assertEqual(sent, [("clip-a", email) for email in self.emails])
        self.assertEqual(snapshot["totals"]["ok_sends"], 2)
        self.assertEqual(self.mark.call_count, 2)
        reconcile.assert_called_once_with(refresh=False)
        self.cleanup.assert_not_called()

    def test_skipped_account_does_not_become_a_confirmed_delivery(self):
        self.send.side_effect = lambda item, account, *a, **k: {
            "email": account.email, "ok": True, "skipped": True, "reason": "account_too_young"}
        response = self.client.post("/api/campaigns", json=self.body)
        self.assertEqual(response.status_code, 200)
        self.finish()
        self.mark.assert_not_called()

    def test_recovered_upload_finishes_campaign_without_new_remote_session(self):
        rows = [{"session_id": f"old-{index}", "chunk_index": 0, "expected_chunk_count": 1,
                 "account_email": email, "org_key": "org", "task_id": "task",
                 "upload_id": f"accepted-old-upload-{index}",
                 "state": "done", "phase": "done", "finalized": True,
                 "campaign_context": {"registry_key": "key", "clip_uid": "clip"}}
                for index, email in enumerate(self.emails)]
        self.send.side_effect = REAL_UPLOAD_TO_ACCOUNT
        with patch.object(campaign.org_policy, "account_kind", return_value="claru"), \
             patch.object(campaign.device_profile, "get_profile", return_value=Mock()), \
             patch.object(campaign, "list_sidecars", side_effect=lambda: rows), \
             patch.object(campaign, "save_sidecar"), \
             patch.object(campaign, "pump_pending", return_value=[]), \
             patch.object(campaign, "_new_identity", side_effect=AssertionError("new session forbidden")), \
             patch.object(campaign, "upload_session", side_effect=AssertionError("network forbidden")) as upload:
            response = self.client.post("/api/campaigns", json=self.body)
            self.assertEqual(response.status_code, 200)
            snapshot, history = self.finish()
        upload.assert_not_called()
        self.assertEqual(history["status"], "done")
        self.assertEqual(snapshot["totals"]["ok_sends"], 2)
        self.assertTrue(all(account["recovered"] for account in history["items"][0]["accounts"]))

    def _run_transport_recovery_campaign(self, *, recovery_succeeds, stop_during_backoff=False,
                                         mixed_owner=False, first_pass="transport",
                                         terminal_retry=False, account_max_attempts=3,
                                         precreate_retry=False):
        candidates = [
            {"clip_uid": uid, "dur_s": 300, "source": "ego4d"}
            for uid in ("clip-a", "clip-b")
        ]
        rows = []
        order = []
        stop = threading.Event()
        pump_calls = []
        send_pairs = []
        retry_links = []
        retry_recorded_at = []
        precreate_attempts = {}
        profile = Mock()
        engine_errors = []

        def prepare(clip, *_args, **_kwargs):
            order.append(("prepare", clip["clip_uid"]))
            path = self.root / f"{clip['clip_uid']}.mp4"
            path.write_bytes(b"inert fixture media")
            return {"clip_uid": clip["clip_uid"], "duration_ms": 300000,
                    "video_path": str(path), "imu_real": True}

        def send(item, account, *args, **kwargs):
            clip_uid = item["clip_uid"]
            send_pairs.append((clip_uid, account.email))
            order.append(("send", clip_uid, account.email))
            # The real upload wrapper owns the account's Session cache. Populate
            # that same seam so the campaign's recovery branch can use it.
            kwargs["session_cache"][account.email] = Mock(email=account.email)
            if clip_uid == "clip-a" and account.email == self.emails[0]:
                if precreate_retry:
                    attempt = precreate_attempts.get(account.email, 0) + 1
                    precreate_attempts[account.email] = attempt
                    if attempt == 1:
                        return {"email": account.email, "org_key": account.org_key,
                                "ok": False, "retryable": True,
                                "error": "Os registros de envio estão ocupados; tente novamente."}
                    if attempt == 2:
                        return {"email": account.email, "org_key": account.org_key,
                                "ok": True, "finalized": True,
                                "session_id": "sid-after-contention",
                                "uploads": ["upload-after-contention"]}
                retry_of = item.get("_retry_of_session_id")
                if retry_of is not None:
                    retry_links.append(retry_of)
                    retry_recorded_at.append(kwargs.get("recorded_at"))
                    rows.append({
                        "session_id": "sid-b", "chunk_index": 0,
                        "expected_chunk_count": 1, "account_email": account.email,
                        "org_key": "org", "task_id": "task", "upload_id": "upload-b",
                        "state": "done", "phase": "done", "finalized": True,
                        "campaign_context": {"registry_key": item["registry_key"],
                                             "clip_uid": clip_uid, "task_id": "task",
                                             "retry_of_session_id": retry_of},
                    })
                    return {"email": account.email, "org_key": account.org_key,
                            "ok": True, "finalized": True, "session_id": "sid-b",
                            "uploads": ["upload-b"]}
                if not rows:
                    recorded_at = upload._iso_now()
                    row = {"session_id": "sid-a", "chunk_index": 0,
                           "expected_chunk_count": 1, "account_email": account.email,
                           "org_key": "org", "task_id": "task", "upload_id": "upload-a",
                           "state": "failed", "phase": "transport",
                           "duration_ms": 300000, "recorded_at": recorded_at,
                           "log_id": "sid-a_0", "filename": "sid-a_0.mp4",
                           "create_attempted": True, "register_first": True,
                           "native_response_schema": True, "finalized": False,
                           "campaign_reconciled": False,
                           "campaign_context": {"registry_key": item["registry_key"],
                                                "clip_uid": clip_uid, "task_id": "task"},
                           "local_video_path": str(item["video_path"]),
                           "video_content_sha256": "fixture-sha256"}
                    rows.append(row)
                    if mixed_owner:
                        rows.append({**row, "account_email": self.emails[1],
                                     "org_key": "foreign-org", "upload_id": "foreign-upload"})
                return {"email": account.email, "org_key": account.org_key,
                        "ok": False, "session_id": "sid-a", "uploads": ["upload-a"],
                        "error": "PUT Blob falhou (6): temporário"}
            return {"email": account.email, "org_key": account.org_key,
                    "ok": True, "finalized": True}

        def pump(session, account_email, org_key, **kwargs):
            pump_calls.append((session, account_email, org_key, dict(kwargs)))
            order.append(("pump", set(kwargs.get("session_ids") or ())))
            self.assertEqual(account_email, self.emails[0])
            self.assertEqual(org_key, "org")
            self.assertEqual(kwargs.get("session_ids"), {"sid-a"})
            self.assertEqual(getattr(session, "email", None), self.emails[0])
            if len(pump_calls) == 1 and first_pass in {"finalize", "complete"}:
                rows[0].update({"state": "completing", "phase": first_pass,
                                "finalize_requested": True, "finalized": False,
                                "evaluation_required": False, "evaluation_verified": False})
            elif len(pump_calls) == 1 and first_pass == "429":
                rows[0].update({"state": "quarantine", "phase": "evaluation_review",
                                "finalize_requested": True, "finalized": False,
                                "evaluation_required": True, "evaluation_verified": False,
                                "evaluation_http_status": 429,
                                "error": "Avaliação inconclusiva (HTTP 429); envio preservado para revisão."})
            if len(pump_calls) == 1 and terminal_retry:
                recorded_at = rows[0]["recorded_at"]
                receipt = {"uploadId": "upload-a", "sessionId": "sid-a",
                           "logId": "sid-a_0", "orgResourceKey": "org",
                           "userResourceKey": "user-resource-a",
                           "userEmail": self.emails[0], "taskId": "task",
                           "taskName": "Furniture Assembly", "recordedAt": recorded_at,
                           "createdAt": recorded_at, "durationMs": 300000,
                           "status": "failed", "orgName": "Org",
                           "storageAccount": "storage", "meta": {}}
                upload.mark_remote_terminal_failure(
                    rows[0], receipt, Mock(email=self.emails[0]),
                    owner_resource_key="user-resource-a", checked_at=upload._iso_now())
            if recovery_succeeds and len(pump_calls) == 2:
                rows[0].update({"state": "done", "phase": "done",
                                "finalize_requested": True, "finalized": True,
                                "evaluation_required": False, "evaluation_verified": False})
            return list(rows)

        def reconcile(selected_rows, account, item, task_id):
            order.append(("reconcile", account.email, item["clip_uid"]))
            if (account.email == self.emails[0] and item["clip_uid"] == "clip-a"
                    and selected_rows and selected_rows[0].get("finalized") is True):
                return {"email": account.email, "org_key": account.org_key,
                        "ok": True, "finalized": True, "session_id": "sid-a",
                        "uploads": ["upload-a"]}
            return None

        def run(cfg, **kwargs):
            try:
                return campaign.run_campaign(
                    replace(cfg, realistic_timeline=False, allow_new_accounts=True,
                            shuffle_schedule=False, account_workers=1,
                            account_max_attempts=account_max_attempts, account_retry_s=0,
                            cleanup_after_upload=True), **kwargs)
            except Exception as exc:
                engine_errors.append(f"{type(exc).__name__}: {exc}")
                raise

        def interruptible_sleep(*_args, **_kwargs):
            if stop_during_backoff:
                stop.set()
                self.instance._stop.set()
                return True
            return False

        self.prepare.side_effect = prepare
        self.send.side_effect = send
        self.cleanup.side_effect = lambda *a, **k: (
            order.append(("cleanup",)) or {"files": 1, "bytes": 10,
                                           "errors": [], "protected": 0})
        with patch.object(runner, "run_campaign", side_effect=run), \
             patch.object(campaign, "_compatible_task_clips", return_value=candidates), \
             patch.object(campaign, "_ego_clip_inputs", side_effect=lambda clip: (clip, object())), \
             patch.object(campaign, "list_sidecars", side_effect=lambda: list(rows)), \
             patch.object(campaign, "is_pending_transport",
                          side_effect=lambda row: row.get("phase") == "transport"), \
             patch.object(campaign, "_pump_account_pending", side_effect=pump), \
             patch.object(campaign, "_reconcile_uploads", side_effect=reconcile), \
             patch.object(campaign, "_acknowledge_campaign_upload"), \
             patch.object(campaign.device_profile, "get_profile", return_value=profile), \
             patch.object(campaign, "_interruptible_sleep", side_effect=interruptible_sleep), \
             patch.object(campaign, "_tick_every", return_value=0):
            response = self.client.post("/api/campaigns", json={**self.body, "count": 2,
                                                                  "cleanup_after_upload": True})
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(response.get_json().get("started", True), True,
                             response.get_json())
            try:
                snapshot, history = self.finish()
            except AssertionError as exc:
                raise AssertionError(f"{exc}; engine errors: {engine_errors}") from exc
        return (snapshot, history, rows, order, pump_calls, send_pairs, stop,
                retry_links, retry_recorded_at)

    def test_transport_recovery_reuses_receipt_then_continues_after_cleanup(self):
        snapshot, history, rows, order, pump_calls, send_pairs, _stop, _retry_links, _retry_recorded = \
            self._run_transport_recovery_campaign(recovery_succeeds=True)
        self.assertEqual(history["status"], "done")
        self.assertEqual([item["clip_uid"] for item in history["items"]], ["clip-a", "clip-b"])
        self.assertEqual(send_pairs.count(("clip-a", self.emails[0])), 1)
        self.assertEqual(send_pairs.count(("clip-a", self.emails[1])), 1)
        self.assertEqual(send_pairs.count(("clip-b", self.emails[0])), 1)
        self.assertEqual(send_pairs.count(("clip-b", self.emails[1])), 1)
        self.assertEqual(len(pump_calls), 2)
        self.assertEqual([call[3]["session_ids"] for call in pump_calls],
                         [{"sid-a"}, {"sid-a"}])
        self.assertTrue(all(call[3]["profile"] is not None for call in pump_calls))
        self.assertTrue(rows[0]["finalized"])
        first_cleanup = order.index(("cleanup",))
        self.assertLess(order.index(("reconcile", self.emails[0], "clip-a")), first_cleanup)
        self.assertLess(order.index(("send", "clip-a", self.emails[1])), first_cleanup)
        self.assertLess(first_cleanup, order.index(("prepare", "clip-b")))
        self.assertEqual(snapshot["totals"]["ok_sends"], 4)

    def test_retryable_journal_contention_retries_account_before_abandoning_batch(self):
        snapshot, history, _rows, _order, pump_calls, send_pairs, _stop, _links, _recorded = \
            self._run_transport_recovery_campaign(
                recovery_succeeds=False, precreate_retry=True)
        self.assertEqual(history["status"], "done")
        self.assertEqual([item["clip_uid"] for item in history["items"]], ["clip-a", "clip-b"])
        self.assertEqual(send_pairs.count(("clip-a", self.emails[0])), 2)
        self.assertEqual(send_pairs.count(("clip-a", self.emails[1])), 1)
        self.assertEqual(pump_calls, [])
        self.assertEqual(snapshot["totals"]["ok_sends"], 4)

    def test_transport_recovery_resumes_pending_finalize_on_same_receipt(self):
        _snapshot, history, rows, order, pump_calls, send_pairs, _stop, _retry_links, _retry_recorded = \
            self._run_transport_recovery_campaign(recovery_succeeds=True,
                                                  first_pass="finalize")
        self.assertEqual(history["status"], "done")
        self.assertEqual([item["clip_uid"] for item in history["items"]], ["clip-a", "clip-b"])
        self.assertEqual(len(pump_calls), 2)
        self.assertEqual([call[3]["session_ids"] for call in pump_calls],
                         [{"sid-a"}, {"sid-a"}])
        self.assertEqual(send_pairs.count(("clip-a", self.emails[0])), 1)
        self.assertTrue(rows[0]["finalized"])
        first_cleanup = order.index(("cleanup",))
        self.assertLess(first_cleanup, order.index(("prepare", "clip-b")))

    def test_transport_recovery_resumes_pending_complete_on_same_receipt(self):
        _snapshot, history, rows, order, pump_calls, send_pairs, _stop, _retry_links, _retry_recorded = \
            self._run_transport_recovery_campaign(recovery_succeeds=True,
                                                  first_pass="complete")
        self.assertEqual(history["status"], "done")
        self.assertEqual([item["clip_uid"] for item in history["items"]], ["clip-a", "clip-b"])
        self.assertEqual(len(pump_calls), 2)
        self.assertEqual([call[3]["session_ids"] for call in pump_calls],
                         [{"sid-a"}, {"sid-a"}])
        self.assertEqual(send_pairs.count(("clip-a", self.emails[0])), 1)
        self.assertTrue(rows[0]["finalized"])
        first_cleanup = order.index(("cleanup",))
        self.assertLess(first_cleanup, order.index(("prepare", "clip-b")))

    def test_transport_recovery_does_not_retry_http_429_or_clear_pending_receipt(self):
        snapshot, history, rows, _order, pump_calls, send_pairs, _stop, _retry_links, _retry_recorded = \
            self._run_transport_recovery_campaign(recovery_succeeds=False, first_pass="429")
        self.assertEqual(history["status"], "error")
        self.assertEqual(len(history["items"]), 1)
        self.assertEqual(len(pump_calls), 1)
        self.assertEqual(send_pairs.count(("clip-a", self.emails[0])), 1)
        self.assertNotIn(("clip-b", self.emails[0]), send_pairs)
        self.assertEqual(rows[0]["state"], "quarantine")
        self.assertEqual(rows[0]["phase"], "evaluation_review")
        self.assertEqual(rows[0]["evaluation_http_status"], 429)
        self.assertFalse(rows[0]["finalized"])
        self.assertTrue((self.root / "clip-a.mp4").is_file())
        self.cleanup.assert_not_called()
        self.assertEqual(snapshot["totals"]["ok_sends"], 1)

    def test_terminal_remote_failure_allows_one_linked_fresh_sid_within_total_budget(self):
        snapshot, history, rows, order, pump_calls, send_pairs, _stop, retry_links, retry_recorded_at = \
            self._run_transport_recovery_campaign(recovery_succeeds=False,
                                                  terminal_retry=True)
        self.assertEqual(history["status"], "done")
        self.assertEqual([item["clip_uid"] for item in history["items"]], ["clip-a", "clip-b"])
        self.assertEqual(len(pump_calls), 1)
        self.assertEqual(send_pairs.count(("clip-a", self.emails[0])), 2)
        self.assertEqual(send_pairs.count(("clip-a", self.emails[1])), 1)
        self.assertEqual(retry_links, ["sid-a"])
        self.assertEqual(retry_recorded_at, [None])
        self.assertEqual(rows[0]["phase"], "remote_terminal_failure")
        self.assertEqual(rows[1]["session_id"], "sid-b")
        self.assertEqual(rows[1]["campaign_context"]["retry_of_session_id"], "sid-a")
        account = AccountSpec(self.emails[0], "org")
        item = {"clip_uid": "clip-a", "registry_key": rows[0]["campaign_context"]["registry_key"]}
        self.assertEqual(campaign._terminal_retry_parent(rows, account, item, "task"), "sid-a")
        with patch.object(campaign.sent_registry, "recovery_reset_checker",
                          return_value=lambda *_args: False), \
             patch.object(campaign.sent_registry, "mark_sent_many") as mark_many, \
             patch.object(campaign, "_acknowledge_campaign_upload") as acknowledge:
            self.assertIsNone(campaign._reconcile_uploads([rows[0]], account, item, "task"))
        mark_many.assert_not_called()
        acknowledge.assert_not_called()
        self.assertEqual(order.count(("cleanup",)), 2)
        self.assertEqual(snapshot["totals"]["ok_sends"], 4)

    def test_terminal_remote_failure_with_spent_budget_preserves_media_and_stops(self):
        snapshot, history, rows, _order, pump_calls, send_pairs, _stop, retry_links, _retry_recorded = \
            self._run_transport_recovery_campaign(
                recovery_succeeds=False, terminal_retry=True, account_max_attempts=2)
        self.assertEqual(history["status"], "error")
        self.assertEqual([item["clip_uid"] for item in history["items"]], ["clip-a"])
        self.assertEqual(len(pump_calls), 1)
        self.assertEqual(send_pairs.count(("clip-a", self.emails[0])), 1)
        self.assertEqual(retry_links, [])
        self.assertEqual(rows[0]["phase"], "remote_terminal_failure")
        self.assertTrue(any("orçamento de tentativas acabou" in str(account.get("error"))
                            for account in history["items"][0]["accounts"]))
        self.cleanup.assert_not_called()
        self.assertEqual(snapshot["totals"]["ok_sends"], 1)

    def test_exhausted_transport_recovery_preserves_pending_and_blocks_next_clip(self):
        snapshot, history, rows, _order, pump_calls, send_pairs, _stop, _retry_links, _retry_recorded = \
            self._run_transport_recovery_campaign(recovery_succeeds=False)
        self.assertEqual(history["status"], "error")
        self.assertEqual([item["clip_uid"] for item in history["items"]], ["clip-a"])
        self.assertEqual(len(pump_calls), 2)
        self.assertEqual(send_pairs.count(("clip-a", self.emails[0])), 1)
        self.assertEqual(send_pairs.count(("clip-a", self.emails[1])), 1)
        self.assertNotIn(("clip-b", self.emails[0]), send_pairs)
        self.assertNotIn(("clip-b", self.emails[1]), send_pairs)
        self.assertEqual(rows[0]["session_id"], "sid-a")
        self.assertEqual(rows[0]["upload_id"], "upload-a")
        self.assertEqual(rows[0]["phase"], "transport")
        self.assertTrue((self.root / "clip-a.mp4").is_file())
        self.cleanup.assert_not_called()
        self.assertEqual(snapshot["totals"]["ok_sends"], 1)

    def test_transport_recovery_stops_during_backoff_without_new_send(self):
        _snapshot, history, _rows, _order, pump_calls, send_pairs, stopped, _retry_links, _retry_recorded = \
            self._run_transport_recovery_campaign(recovery_succeeds=False,
                                                  stop_during_backoff=True)
        self.assertTrue(stopped.is_set())
        self.assertEqual(pump_calls, [])
        self.assertEqual(send_pairs, [("clip-a", self.emails[0])])
        self.assertEqual([item["clip_uid"] for item in history["items"]], ["clip-a"])

    def test_mixed_owner_transport_journal_is_not_recovered(self):
        _snapshot, history, rows, _order, pump_calls, send_pairs, _stop, _retry_links, _retry_recorded = \
            self._run_transport_recovery_campaign(recovery_succeeds=False, mixed_owner=True)
        self.assertEqual(history["status"], "error")
        self.assertEqual(pump_calls, [])
        self.assertEqual(rows[0]["phase"], "transport")
        self.assertEqual(rows[1]["account_email"], self.emails[1])
        self.assertEqual(rows[1]["org_key"], "foreign-org")
        self.assertEqual(send_pairs.count(("clip-a", self.emails[0])), 1)
        self.assertNotIn(("clip-b", self.emails[0]), send_pairs)

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
        self.stack.enter_context(patch("moneymin.ego_accelerator.ready_scenario_clips", return_value=[]))
        self.stack.enter_context(patch.object(campaign, "_clip_is_cached", return_value=False))
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
        self.client = server.create_app(for_testing=True).test_client()
        self.body = {"run_until_exhausted": False, "accounts": self.emails, "tasks": [{"task_id": "task"}], "dataset": "ego4d"}

    def finish(self):
        self.instance._thread.join(5)
        self.assertFalse(self.instance._thread.is_alive(), "campanha não encerrou")
        snap = self.client.get("/api/campaigns/current").get_json()
        logs = list(self.root.glob("campaign_*.json"))
        self.assertEqual(len(logs), 1, snap)
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

    def test_original_source_provenance_survives_local_history_and_progress(self):
        origin = {'dataset': 'ego4d', 'recording_origin': 'third_party_dataset',
                  'parent_video_uid': 'original-parent', 'window_s': [10.123, 310.123],
                  'original_device': 'original-camera',
                  'imu': {'processing': 'resampled_measured_signals', 'native_rate_hz': None}}
        self.prepare.side_effect = lambda *a, **k: {
            'duration_ms': 300000, 'video_path': str(self.root / 'fixture.mp4'),
            'imu_real': True, 'source_provenance': origin}
        with patch.object(self.instance, '_on_event', wraps=self.instance._on_event) as events:
            response = self.client.post('/api/campaigns', json=self.body)
            self.assertEqual(response.status_code, 200)
            snapshot, history = self.finish()
        self.assertEqual(history['items'][0]['source_provenance'], origin)
        event = next(call.args[1] for call in events.call_args_list if call.args[0] == 'clip_ready')
        self.assertEqual(event['source_provenance'], origin)
        self.assertTrue(any(row['kind'] == 'clip_ready' for row in snapshot['events']))

    def test_campaign_waits_for_selected_account_recovery(self):
        with patch.object(server.recovery, "snapshot", return_value={"items": [{"email": self.emails[0]}]}):
            response = self.client.post("/api/campaigns", json=self.body)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["recovery_accounts"], [self.emails[0]])
        self.prepare.assert_not_called()
        self.send.assert_not_called()

    def test_reviewed_candidates_survive_catalog_change_without_new_selection(self):
        body = {**self.body, "include_clip_plan": True}
        response = self.client.post("/api/campaigns/preflight", json=body)
        self.assertEqual(response.status_code, 200)
        review = response.get_json()
        self.assertTrue(review["ok"], review)
        self.assertEqual(review["clip_plan"][0]["clip_uid"], "clip")
        self.assertEqual(review["clip_plan"][0]["eligible_accounts"], self.emails)
        self.prepare.assert_not_called()
        self.send.assert_not_called()
        with patch.object(campaign, "_compatible_task_clips", side_effect=AssertionError("catalog must stay frozen")):
            started = self.client.post("/api/campaigns", json={**body, "preflight_id": review["preflight_id"]})
            self.assertEqual(started.status_code, 200, started.get_json())
            snapshot, log = self.finish()
        self.assertEqual(snapshot["state"], "done")
        self.assertEqual([item["clip_uid"] for item in log["items"]], ["clip"])

    def test_review_blocks_when_new_content_cannot_fill_hours_goal(self):
        (self.root / "sent_videos.json").write_text(json.dumps({
            "minute|task|Furniture Assembly": {"clip": [self.emails[0]]}}), encoding="utf-8")
        review = self.client.post("/api/campaigns/preflight", json={
            **self.body, "include_clip_plan": True, "target_hours": 1}).get_json()
        self.assertFalse(review["ok"], review)
        self.assertIsNone(review["preflight_id"])
        blocker = next(w for w in review["blockers"] if "Conteúdo novo insuficiente" in w)
        self.assertIn("2 conta(s)", blocker)
        self.assertIn("0.00–0.08 h", blocker)
        self.assertEqual(review["estimated_sends"], 1)
        self.prepare.assert_not_called()
        self.send.assert_not_called()

    def test_preflight_counts_permitted_overlap_as_admitted_delivery_duration(self):
        clips = [dict(clip_uid=uid, parent_video_uid="same-parent", source="ego4d",
                      dur_s=300, window_s=window)
                 for uid, window in (("one", [0, 300]), ("two", [150, 450]))]
        with patch.object(campaign, "_compatible_task_clips", return_value=clips):
            review = self.client.post("/api/campaigns/preflight", json={
                **self.body, "include_clip_plan": True, "target_hours": 0.15}).get_json()
        self.assertTrue(review["ok"], review)
        self.assertTrue(review["preflight_id"])
        self.assertEqual(review["capacity"]["available_seconds_min"], 600)
        self.assertEqual(review["capacity"]["accounts"][0]["unique_footage_seconds"], 450)
        self.prepare.assert_not_called()
        self.send.assert_not_called()

    def test_completed_campaign_retains_review_identity_after_lost_start_response(self):
        body = {**self.body, "include_clip_plan": True}
        review = self.client.post("/api/campaigns/preflight", json=body).get_json()
        self.assertTrue(review["ok"], review)
        response = self.client.post("/api/campaigns", json={**body, "preflight_id": review["preflight_id"]})
        self.assertEqual(response.status_code, 200)
        snapshot, _ = self.finish()
        self.assertEqual(snapshot["state"], "done")
        self.assertEqual(snapshot["start_request_id"], review["preflight_id"])
        self.assertEqual(self.client.get("/api/campaigns/current").get_json()["start_request_id"], review["preflight_id"])

    def test_reset_invalidates_reviewed_account_clip_eligibility(self):
        registry = self.root / "sent_videos.json"
        registry.write_text(json.dumps({"minute|task|Furniture Assembly": {"clip": [self.emails[0]]}}), encoding="utf-8")
        body = {**self.body, "include_clip_plan": True}
        review = self.client.post("/api/campaigns/preflight", json=body).get_json()
        self.assertTrue(review["ok"], review)
        self.assertEqual(review["clip_plan"][0]["excluded_accounts"], [self.emails[0]])
        self.assertEqual(self.client.post("/api/sent/reset", json={}).status_code, 200)
        start = self.client.post("/api/campaigns", json={**body, "preflight_id": review["preflight_id"]})
        self.assertEqual(start.status_code, 409)
        self.assertEqual(start.get_json()["error_code"], "preflight_history_changed")
        self.prepare.assert_not_called()
        self.send.assert_not_called()

    def test_review_blocks_exhausted_pool_without_preparing_media(self):
        (self.root / "sent_videos.json").write_text(json.dumps({
            "minute|task|Furniture Assembly": {"clip": self.emails}}), encoding="utf-8")
        review = self.client.post("/api/campaigns/preflight", json={**self.body, "include_clip_plan": True}).get_json()
        self.assertFalse(review["ok"])
        self.assertIsNone(review["preflight_id"])
        self.assertEqual(review["clip_plan"][0]["excluded_accounts"], self.emails)
        self.prepare.assert_not_called()
        self.send.assert_not_called()

    def test_review_exposes_only_public_candidate_fields(self):
        with patch.object(campaign, "_compatible_task_clips", return_value=[{
                "clip_uid": "clip", "dur_s": 300, "source": "ego4d",
                "private_url": "https://private.example/secret-token", "video_path": "C:/private/customer"}]):
            response = self.client.post("/api/campaigns/preflight", json={**self.body, "include_clip_plan": True})
        self.assertTrue(response.get_json()["ok"])
        self.assertNotIn("secret-token", response.get_data(as_text=True))
        self.assertNotIn("C:/private", response.get_data(as_text=True))

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

    def test_preview_and_start_reject_invalid_parameters_without_remote_work(self):
        invalid = [[], "unexpected", {**self.body, "accounts": "a@example.com"},
                   {**self.body, "accounts": [None]}, {**self.body, "accounts": [self.emails[0]] * 2},
                   {**self.body, "tasks": [None]}, {**self.body, "target_hours": "nan"},
                   {**self.body, "target_hours": "inf"}, {**self.body, "delay_s": "nan"},
                   {**self.body, "cleanup_after_upload": "true"},
                   {**self.body, "active_hours": {"start": 7, "end": 18}},
                   {**self.body, "active_hours": [7.5, 18]}, {**self.body, "active_hours": "78"},
                   {**self.body, "active_hours": [True, 18]}, {**self.body, "delay_mode": "bad"}]
        with patch.object(server, "_resolve_org") as resolve, patch.object(self.instance, "start") as start:
            for body in invalid:
                for path in ("/api/campaigns/preflight", "/api/campaigns"):
                    with self.subTest(path=path, body=body):
                        self.assertEqual(self.client.post(path, json=body).status_code, 400)
        resolve.assert_not_called()
        start.assert_not_called()
        self.prepare.assert_not_called()
        self.send.assert_not_called()

    def test_upload_concurrency_is_validated_and_forwarded(self):
        for invalid in (0, 16, 1.5, True, "3"):
            with self.subTest(invalid=invalid):
                body = {**self.body, "account_workers": invalid}
                self.assertEqual(self.client.post("/api/campaigns/preflight", json=body).status_code, 400)
                self.assertEqual(self.client.post("/api/campaigns", json=body).status_code, 400)
        body = {**self.body, "account_workers": 2}
        preflight = self.client.post("/api/campaigns/preflight", json=body)
        self.assertEqual(preflight.status_code, 200)
        self.assertEqual(preflight.get_json()["account_workers"], 2)
        with patch.object(self.instance, "start") as start:
            response = self.client.post("/api/campaigns", json=body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(start.call_args.args[0].account_workers, 2)

    def test_upload_waits_when_active_window_closes_before_launch(self):
        waits = []
        checks = [3600, 3600]

        def remaining(*_args):
            return checks.pop(0) if checks else 0

        def wait_for_window(*_args):
            waits.append(True)
            return False

        def send(item, account, *args, **kwargs):
            self.assertGreaterEqual(len(waits), 2)
            return {"email": account.email, "ok": True, "finalized": True}

        self.send.side_effect = send
        with patch.object(campaign, "_window_remaining_s", side_effect=remaining), \
             patch.object(campaign, "_wait_for_window", side_effect=wait_for_window):
            response = self.client.post(
                "/api/campaigns", json={**self.body, "active_hours": [7, 18]})
            self.assertEqual(response.status_code, 200)
            self.finish()
        self.assertGreaterEqual(len(waits), 2)

    def test_preflight_checks_other_accounts_concurrently(self):
        emails = [*self.emails, "c@example.com"]
        barrier = threading.Barrier(2)

        def session_for(_email):
            session = Mock()
            def tasks(_org):
                barrier.wait(3)
                return [{"id": "task"}]
            session.all_tasks.side_effect = tasks
            return session

        with patch.object(server, "_list_accounts", return_value=[{"email": e} for e in emails]), \
             patch.object(server.Session, "from_email", side_effect=session_for):
            response = self.client.post("/api/campaigns/preflight", json={
                **self.body, "accounts": emails})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["accounts"]["validated"], 3)

    def test_explicit_retention_keeps_prepared_cache_after_successful_campaign(self):
        with patch("moneymin.ego_accelerator.configured_budget_gb", return_value=400), \
             patch("moneymin.ego_accelerator.ready_scenario_clips", return_value=[]), \
             patch.object(campaign, "_clip_is_cached", return_value=True), \
             patch.object(campaign, "_enforce_account_video_cache", return_value=(0, 0)):
            response = self.client.post("/api/campaigns", json={**self.body, "cleanup_after_upload": False})
            self.assertEqual(response.status_code, 200, response.get_json())
            snap, log = self.finish()
        self.assertEqual((snap["state"], log["status"]), ("done", "done"))
        self.cleanup.assert_not_called()
        self.assertEqual(self.prepare.call_count, 1)

    def test_explicit_retention_keeps_prepared_ego_cache_without_budget_file(self):
        with patch("moneymin.ego_accelerator.configured_budget_gb", return_value=0), \
             patch.object(campaign, "_clip_is_cached", return_value=True), \
             patch.object(campaign, "_enforce_account_video_cache", return_value=(0, 0)):
            response = self.client.post("/api/campaigns", json={**self.body, "cleanup_after_upload": False})
            self.assertEqual(response.status_code, 200, response.get_json())
            snap, log = self.finish()
        self.assertEqual((snap["state"], log["status"]), ("done", "done"))
        self.cleanup.assert_not_called()

    def test_prepared_holo_cache_survives_without_ego_budget(self):
        holo = {"clip_uid": "holoassist:clip", "video_name": "clip",
                "dur_s": 300, "source": "holoassist"}
        with patch("moneymin.web.server._load_prefs", return_value={}), \
             patch("moneymin.ego_accelerator.configured_budget_gb", return_value=0), \
             patch.object(campaign.holoassist, "list_clips", return_value=[holo]), \
             patch.object(campaign, "_clip_is_cached", return_value=True), \
             patch.object(campaign, "prepare_holoassist_clip", return_value={
                 "duration_ms": 300000, "video_path": str(self.root / "holo.mp4"),
                 "imu_real": True}), \
             patch.object(campaign, "_enforce_account_video_cache", return_value=(0, 0)):
            response = self.client.post("/api/campaigns", json={**self.body, "dataset": "holoassist",
                                                                         "cleanup_after_upload": False})
            self.assertEqual(response.status_code, 200, response.get_json())
            snap, log = self.finish()
        self.assertEqual((snap["state"], log["status"]), ("done", "done"))
        self.cleanup.assert_not_called()
        self.prepare.assert_not_called()

    def test_mixed_cache_and_dataset_uses_cache_first_and_releases_each_managed_item(self):
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
             patch.object(campaign.nymeria, "automatic_candidates", return_value=[]), \
             patch("moneymin.ego_accelerator.configured_budget_gb", return_value=400), \
             patch("moneymin.ego_accelerator.ready_scenario_clips", return_value=[]), \
             patch.object(campaign, "_clip_is_cached", side_effect=lambda clip, _: clip["clip_uid"] == "local"), \
             patch.object(campaign, "_enforce_account_video_cache", return_value=(0, 0)):
            response = self.client.post("/api/campaigns", json={**self.body, "dataset": "all", "count": 2})
            self.assertEqual(response.status_code, 200, response.get_json())
            snap, log = self.finish()
        self.assertEqual((snap["state"], log["status"]), ("done", "done"))
        self.assertEqual([item["clip_uid"] for item in log["items"]], ["local", "remote"])
        self.assertEqual(self.cleanup.call_count, 2)

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
        self.cleanup.assert_called_once()
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

    def test_unreachable_hours_goal_is_rejected_before_campaign(self):
        response = self.client.post("/api/campaigns", json={**self.body, "target_hours": 1})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error_code"], "campaign_capacity_insufficient")
        self.assertFalse(self.instance.running)
        self.assertEqual(list(self.root.glob("campaign_*.json")), [])
        self.prepare.assert_not_called()
        self.send.assert_not_called()

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
             patch("moneymin.ego_accelerator.ready_scenario_clips", return_value=[]), \
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
            return {"email": account.email, "ok": True, "finalized": True}
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
            return {"email": account.email, "ok": True, "finalized": True}
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
        self.cleanup.assert_called_once()

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
            "finalized": account.email == self.emails[0],
            "error": "policy refused", "retryable": False}
        self.client.post("/api/campaigns", json=self.body)
        snap, log = self.finish()
        self.assertEqual(log["status"], "error")
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
        self.cleanup.assert_called_once()

    def test_stop_drains_current_send_and_prevents_next_account(self):
        entered, release = threading.Event(), threading.Event()
        def send(item, account, *args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test release timeout")
            return {"email": account.email, "ok": True, "finalized": True}
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
        self.cleanup.assert_called_once()
        self.assertFalse(any(e["kind"] == "campaign_done" for e in snap["events"]))

    def test_pause_holds_next_account_slot_until_resume(self):
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()

        def send(item, account, *args, **kwargs):
            if account.email == self.emails[0]:
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test release timeout")
                completed.set()
            return {"email": account.email, "ok": True, "finalized": True}

        self.send.side_effect = send
        started = self.client.post("/api/campaigns", json=self.body)
        self.assertEqual(started.status_code, 200)
        try:
            self.assertTrue(entered.wait(5))
            paused = self.client.post("/api/campaigns/pause")
            self.assertEqual(paused.status_code, 200)
            release.set()
            self.assertTrue(completed.wait(5), "in-flight fixture may finish while paused")
            self.assertEqual(self.send.call_count, 1)
            self.assertTrue(self.instance._thread.is_alive())
            self.assertTrue(self.client.get("/api/campaigns/current").get_json()["pause_requested"])
            self.assertEqual(self.client.post("/api/campaigns/resume").status_code, 200)
        finally:
            release.set()
            if self.instance.pause_requested:
                self.instance.resume()
            self.instance._thread.join(5)
        snapshot, history = self.finish()
        self.assertEqual((snapshot["state"], history["status"]), ("done", "done"))
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(snapshot["totals"]["ok_sends"], 2)

    def test_stop_drains_two_active_workers_and_keeps_third_account_queued(self):
        self.emails.append("queued@example.invalid")
        entered, release = threading.Event(), threading.Event()
        guard = threading.Lock()
        active = []

        def send(item, account, *args, **kwargs):
            with guard:
                active.append(account.email)
                if len(active) == 2:
                    entered.set()
            if not release.wait(5):
                raise RuntimeError("test release timeout")
            return {"email": account.email, "ok": True, "finalized": True}

        self.send.side_effect = send
        with patch.object(server, "_list_accounts", return_value=[{"email": email} for email in self.emails]), \
             patch.object(runner, "run_campaign", side_effect=lambda cfg, **kw: campaign.run_campaign(
                 replace(cfg, realistic_timeline=False, allow_new_accounts=True, shuffle_schedule=False,
                         account_workers=2, account_gap_s=0, account_retry_s=0), **kw)):
            started = self.client.post("/api/campaigns", json=self.body)
            self.assertEqual(started.status_code, 200)
            try:
                self.assertTrue(entered.wait(5))
                stopping = self.client.post("/api/campaigns/stop")
                self.assertEqual(stopping.status_code, 200)
                self.assertEqual(stopping.get_json()["state"], "stopping")
            finally:
                release.set()
                self.instance._thread.join(5)
            snapshot, history = self.finish()
        self.assertEqual((snapshot["state"], history["status"]), ("stopped", "stopped"))
        self.assertEqual(set(active), set(self.emails[:2]))
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual({row["email"] for row in history["items"][0]["accounts"]}, set(self.emails[:2]))
        self.assertEqual(snapshot["totals"]["ok_sends"], 2)
        self.assertEqual(self.mark.call_count, 2)
        self.cleanup.assert_called_once()
        self.assertFalse(any(event["kind"] == "campaign_done" for event in snapshot["events"]))

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
