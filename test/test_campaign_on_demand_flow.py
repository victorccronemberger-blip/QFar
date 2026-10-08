"""Bounded prepare/delivery/eviction scheduling, with inert local providers."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from moneymin import campaign
from moneymin import upload
from moneymin.campaign_types import AccountSpec, CampaignConfig, TaskSpec

REAL_CLEANUP_UPLOADED_ITEM = campaign._cleanup_uploaded_item


class OnDemandCampaignTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.stack.enter_context(patch.object(campaign.config, "DATA_DIR", self.root / "state"))
        self.stack.enter_context(patch.object(campaign.config, "MEDIA_DATA_DIR", self.root))
        self.events = []
        self.accounts = [AccountSpec(email, "fixture-org") for email in
                         ("first@example.invalid", "second@example.invalid")]
        self.task = TaskSpec("fixture-task", "fixture-scenario", 300, 1800, count=2)
        self.clips = [{"clip_uid": uid, "parent_video_uid": uid, "dur_s": 300,
                       "source": "ego4d"} for uid in ("one", "two")]
        self.cfg = CampaignConfig(self.accounts, [self.task], work_dir=self.root / "media",
                                  candidate_plan={self.task.task_id: self.clips},
                                  account_workers=2, shuffle_schedule=False,
                                  cleanup_after_upload=True)
        for name, value in (("sent_emails", set()), ("is_sent_to_all", False)):
            self.stack.enter_context(patch.object(campaign.sent_registry, name, return_value=value))
        self.mark = self.stack.enter_context(patch.object(campaign.sent_registry, "mark_sent"))
        self.stack.enter_context(patch.object(campaign, "_clip_is_cached", return_value=False))
        self.stack.enter_context(patch.object(campaign, "_ego_clip_inputs", side_effect=lambda clip: (clip, {})))
        self.prepare = self.stack.enter_context(patch.object(campaign, "prepare_clip", side_effect=self.prepare_item))
        self.send = self.stack.enter_context(patch.object(campaign, "upload_to_account", side_effect=self.deliver))
        self.cleanup = self.stack.enter_context(patch.object(campaign, "_cleanup_uploaded_item", side_effect=self.release))
        self.prefetch = self.stack.enter_context(patch.object(campaign, "_prefetch_following"))
        self.warm = self.stack.enter_context(patch.object(campaign, "_warm_account_videos"))

    def prepare_item(self, clip, *args, **kwargs):
        self.events.append(("prepare", clip["clip_uid"]))
        return {"duration_ms": 300000, "video_path": str(self.cfg.work_dir / "fixture_native.mp4"),
                "imu_real": True}

    def deliver(self, item, account, *args, **kwargs):
        self.events.append(("deliver", item["clip_uid"], account.email))
        return {"email": account.email, "ok": True, "finalized": True}

    def release(self, item, *args, **kwargs):
        self.events.append(("release", item.get("clip_uid")))
        return {"files": 1, "bytes": 10, "errors": [], "protected": 0}

    def test_next_source_is_prepared_only_after_current_batch_releases(self):
        self.cfg.unique_video = True
        log = campaign.run_campaign(self.cfg)
        self.assertEqual(log.status, "done")
        self.assertLess(self.events.index(("release", "one")), self.events.index(("prepare", "two")))
        self.assertEqual(len([row for row in self.events if row[:2] == ("deliver", "one")]), 2)
        self.prefetch.assert_not_called()
        self.warm.assert_not_called()

    def test_ready_cache_is_released_after_delivery_when_cleanup_is_selected(self):
        with patch.object(campaign, "_clip_is_cached", return_value=True):
            campaign.run_campaign(self.cfg)
        self.assertEqual(self.cleanup.call_count, 2)

    def test_unconfirmed_delivery_preserves_media_and_blocks_next_acquisition(self):
        def fail(item, account, *args, **kwargs):
            self.events.append(("deliver", item["clip_uid"], account.email))
            return {"email": account.email, "ok": False, "session_id": "pending-fixture"}
        self.send.side_effect = fail
        with self.assertRaisesRegex(RuntimeError, "não adquiriu outro vídeo"):
            campaign.run_campaign(self.cfg)
        self.assertEqual([row for row in self.events if row[0] == "prepare"], [("prepare", "one")])
        self.cleanup.assert_not_called()
        self.prefetch.assert_not_called()

    def test_transient_evaluation_reuses_receipt_and_continues_after_confirmation(self):
        journals = []
        events = []
        def deliver(item, account, *args, **kwargs):
            if item['clip_uid'] == 'one' and account is self.accounts[0]:
                kwargs['session_cache'][account.email] = object()
                journals.append({'session_id': 'existing-fixture', 'account_email': account.email,
                    'org_key': account.org_key, 'task_id': self.task.task_id,
                    'chunk_index': 0, 'expected_chunk_count': 1, 'upload_id': 'receipt-fixture',
                    'state': 'quarantine', 'phase': 'evaluation_review', 'finalized': False,
                    'finalize_requested': True, 'evaluation_required': True, 'evaluation_verified': False,
                    'campaign_context': {'registry_key': item['registry_key'], 'clip_uid': 'one'},
                    'error': 'Avaliação inconclusiva (HTTP -1); envio preservado para revisão.'})
                return {'email': account.email, 'ok': False, 'session_id': 'existing-fixture',
                        'uploads': ['receipt-fixture'], 'finalized': False}
            return self.deliver(item, account)
        def recover(session, **kwargs):
            self.assertEqual(kwargs['session_ids'], {'existing-fixture'})
            self.assertEqual(kwargs['account_email'], self.accounts[0].email)
            journals[0].update(state='done', phase='done', finalized=True, evaluation_verified=True)
            return journals
        self.send.side_effect = deliver
        with patch.object(campaign, 'list_sidecars', side_effect=lambda: journals), \
             patch.object(campaign, 'pump_pending', side_effect=recover) as pump, \
             patch.object(campaign, '_acknowledge_campaign_upload'):
            result = campaign.run_campaign(self.cfg, progress=lambda k,p: events.append((k,p)))
        self.assertEqual(result.status, 'done')
        pump.assert_called_once()
        self.assertEqual(self.prepare.call_count, 2)
        self.assertEqual(self.send.call_count, 4)
        self.assertEqual(self.cleanup.call_count, 2)
        self.assertEqual(result.items[0]['accounts'][0]['session_id'], 'existing-fixture')
        self.assertTrue(result.items[0]['accounts'][0]['evaluation_recovered'])
        self.assertEqual(sum(k == 'account_evaluation_recovery' for k,p in events), 1)

    def test_restricted_account_leaves_survivor_running_and_does_not_retain_batch(self):
        def restricted(item, account, *args, **kwargs):
            if account is self.accounts[0]:
                return {"email": account.email, "ok": False, "restriction_confirmed": True}
            return self.deliver(item, account, *args, **kwargs)
        self.send.side_effect = restricted
        campaign.run_campaign(self.cfg)
        self.assertEqual(self.prepare.call_count, 2)
        self.assertEqual(self.cleanup.call_count, 2)
        self.assertEqual([call.args[1].email for call in self.send.call_args_list].count(self.accounts[0].email), 1)

    def test_classified_pre_receipt_failure_keeps_other_accounts_running_without_banning(self):
        events = []
        def unavailable(item, account, *args, **kwargs):
            if account is self.accounts[0]:
                return {'email': account.email, 'ok': False, 'retryable': False, 'access_error': True,
                        'error': 'PermissionError before send'}
            return self.deliver(item, account)
        self.send.side_effect = unavailable
        log = campaign.run_campaign(self.cfg, progress=lambda k,p: events.append((k,p)))
        self.assertEqual(log.status, 'partial')
        self.assertEqual(self.prepare.call_count, 2)
        self.assertEqual(self.cleanup.call_count, 2)
        self.assertEqual([c.args[1].email for c in self.send.call_args_list].count(self.accounts[0].email), 1)
        self.assertEqual(sum(k == 'account_deferred' for k,p in events), 1)
        self.assertFalse(any(k == 'account_excluded' for k,p in events))

    def test_sensor_rejection_releases_unused_managed_item_before_next_candidate(self):
        def prepare(clip, *args, **kwargs):
            if clip["clip_uid"] == "one":
                raise RuntimeError("sem cobertura contínua de IMU")
            return self.prepare_item(clip, *args, **kwargs)
        self.prepare.side_effect = prepare
        with patch.object(campaign, "_try_prepare_imu_carve", return_value=None), \
             patch.object(campaign, "_cleanup_rejected_prepare", return_value={
                 "files": 2, "bytes": 20, "errors": [], "protected": 0}) as rejected:
            campaign.run_campaign(self.cfg)
        rejected.assert_called_once()
        self.assertEqual(self.send.call_count, 2)

    def test_acquisition_failure_never_starts_another_large_download(self):
        self.prepare.side_effect = RuntimeError("provider download incomplete")
        with patch.object(campaign, "_cleanup_rejected_prepare", return_value={
                "files": 0, "bytes": 0, "errors": [], "protected": 0}), \
             self.assertRaisesRegex(RuntimeError, "nenhum outro vídeo foi adquirido"):
            campaign.run_campaign(self.cfg)
        self.assertEqual(self.prepare.call_count, 1)
        self.send.assert_not_called()

    def test_recent_measured_footage_blocks_same_planned_device_window_before_acquisition(self):
        real = {"clip_uid": "nymeria:fixture:400:700", "parent_video_uid": "fixture",
                "source": "nymeria", "dur_s": 300, "window_s": [400, 700],
                "device_window_ns": [1_100_000_000_000, 1_400_000_000_000],
                "source_clock_domain": "aria_DEVICE_TIME_ns"}
        planned = {**real, "clip_uid": "nymeria-planned:fixture:1100:1400",
                   "window_s": [0, 300], "acquisition_required": True}
        second = TaskSpec("second-task", "fixture-scenario", 300, 1800)
        self.cfg.tasks = [self.task, second]
        self.cfg.candidate_plan = {self.task.task_id: [real], second.task_id: [planned]}
        with patch.object(campaign, "prepare_nymeria_clip", side_effect=self.prepare_item) as prepare:
            campaign.run_campaign(self.cfg)
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(self.send.call_count, 2)

    def test_equal_relative_windows_of_different_device_intervals_are_not_duplicates(self):
        real = {"clip_uid": "nymeria:fixture:0:300", "parent_video_uid": "fixture",
                "source": "nymeria", "window_s": [0, 300],
                "device_window_ns": [1_000_000_000_000, 1_300_000_000_000],
                "source_clock_domain": "aria_DEVICE_TIME_ns"}
        planned = {**real, "clip_uid": "nymeria-planned:fixture:2000:2300",
                   "device_window_ns": [2_000_000_000_000, 2_300_000_000_000],
                   "acquisition_required": True}
        self.assertFalse(campaign._batch_footage_overlaps(real, planned))

    def test_receipt_records_only_the_exact_planned_identity_in_fresh_measured_proof(self):
        alias = "nymeria-planned:fixture:1100:1400"
        clip = {"clip_uid": "nymeria:fixture:400:700", "parent_video_uid": "fixture",
                "source": "nymeria", "dur_s": 300, "window_s": [400, 700],
                "acquired_from_planned_clip_uid": alias,
                "dedup_clip_uids": [alias, "unrelated-overlapping-window"]}
        self.cfg.candidate_plan = {self.task.task_id: [clip]}
        def prepare(row, *args, **kwargs):
            return {**self.prepare_item(row), "source": "nymeria", "_content_candidate": row}
        def deliver(item, account, *args, **kwargs):
            return {**self.deliver(item, account), "selection_evidence": {
                "algorithm": "nymeria-atomic-device-v4",
                "acquisition_plan": {"clip_uid": alias}}}
        self.send.side_effect = deliver
        # The authoritative helper's hash/proof validation is covered by its
        # own tests; here the scheduler receives its declared two identities.
        with patch.object(campaign, "prepare_nymeria_clip", side_effect=prepare), \
             patch.object(campaign.sent_registry, "delivery_clip_uids", return_value=[clip["clip_uid"], alias]), \
             patch.object(campaign.sent_registry, "mark_sent_many") as mark_many:
            campaign.run_campaign(self.cfg)
        self.mark.assert_not_called()
        self.assertEqual(mark_many.call_count, 2)
        self.assertEqual({identity for call in mark_many.call_args_list
                          for _key, identity, _email in call.args[0]}, {clip["clip_uid"], alias})

    def test_queued_consumer_of_same_account_variant_keeps_its_entire_chunk_family(self):
        base = self.cfg.work_dir / "fixture_native.mp4"
        variant = base.with_name(base.stem + "_acc12345678.mp4")
        chunk = variant.with_name(variant.stem + "_ch0_fixture.mp4")
        base.parent.mkdir(parents=True)
        for path in (base, variant, chunk):
            path.write_bytes(b"declared inert media")
        row = {"session_id": "receipt", "account_email": self.accounts[0].email,
               "org_key": self.accounts[0].org_key, "upload_id": "accepted-inert",
               "state": "done", "phase": "done", "finalized": True,
               "campaign_reconciled": True, "chunk_index": 0, "expected_chunk_count": 1}
        with patch("moneymin.recovery._groups", return_value=[[row]]), \
             patch("moneymin.campaign_evidence.publication_registered", return_value=True), \
             patch("moneymin.media_lifecycle.cleanup_managed_media") as delete:
            released = campaign._cleanup_confirmed_account_media(
                {"video_path": str(base)}, self.accounts[0],
                {"ok": True, "finalized": True, "session_id": "receipt"},
                [str(variant), str(chunk)], self.cfg.work_dir, protected_paths={base, variant})
        self.assertIsNone(released)
        delete.assert_not_called()
        self.assertTrue(variant.is_file())
        self.assertTrue(chunk.is_file())

    def test_explicit_retention_mode_keeps_prefetch_available(self):
        self.cfg.cleanup_after_upload = False
        campaign.run_campaign(self.cfg)
        self.prefetch.assert_called()
        self.cleanup.assert_not_called()

    def test_stop_during_current_upload_does_not_acquire_a_future_source(self):
        entered, resume, stopped = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def hold(item, account, *args, **kwargs):
            entered.set()
            if not resume.wait(5):
                raise RuntimeError("fixture timed out")
            return self.deliver(item, account, *args, **kwargs)
        self.send.side_effect = hold
        def run():
            try:
                campaign.run_campaign(self.cfg, should_stop=stopped.is_set)
            except Exception as exc:
                errors.append(exc)
        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            stopped.set()
            resume.set()
            worker.join(5)
        finally:
            resume.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.prepare.call_count, 1)
        self.prefetch.assert_not_called()

    def _stop_while_third_account_waits(self, *, pending_first=False, cleanup_failure=False):
        self.accounts = [AccountSpec(f"account{index}@example.invalid", "fixture-org")
                         for index in range(3)]
        self.cfg.accounts = self.accounts
        self.cfg.realistic_timeline = True
        stopped = threading.Event()
        waits = []
        epoch = [0]

        def reserve(email, duration_s, *, now=None):
            epoch[0] += 1
            end = float(now) if epoch[0] < 3 else float(now) + 600
            return campaign.recording_timeline.RecordingSlot(email, float(now), end)

        def stop_at_recording_wait(delay, should_stop, emit, *, kind, tick_every):
            if kind == "recording_wait_tick":
                waits.append(kind)
                stopped.set()
                return True
            return False

        def prepare(clip, *args, **kwargs):
            path = Path(self.cfg.work_dir) / "fixture_native.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"inert generated native video")
            campaign._record_generated_media(path, root=Path(self.cfg.work_dir),
                                             role="native")
            return {"duration_ms": 300000, "video_path": str(path), "imu_real": True}

        def deliver(item, account, *args, **kwargs):
            if pending_first and account is self.accounts[0]:
                upload.save_sidecar({
                    "session_id": "pending-stop-fixture", "chunk_index": 0,
                    "expected_chunk_count": 1, "account_email": account.email,
                    "org_key": account.org_key, "task_id": self.task.task_id,
                    "upload_id": "", "state": "failed", "phase": "create",
                    "create_attempted": True, "finalized": False,
                    "video_path": item["video_path"],
                    "campaign_context": {"registry_key": item["registry_key"],
                                          "clip_uid": item["clip_uid"]},
                })
                return {"email": account.email, "ok": False, "finalized": False,
                        "session_id": "pending-stop-fixture"}
            return {"email": account.email, "ok": True, "finalized": True}

        self.prepare.side_effect = prepare
        self.send.side_effect = deliver
        self.stack.enter_context(patch.object(campaign.recording_timeline, "reserve",
                                              side_effect=reserve))
        self.stack.enter_context(patch.object(campaign, "_interruptible_sleep",
                                              side_effect=stop_at_recording_wait))
        cleanup_patch = (patch.object(campaign, "_cleanup_uploaded_item", return_value={
            "files": 0, "bytes": 0, "errors": ["fixture: PermissionError"],
            "protected": 0, "retained_managed": 1, "retained_bytes": 20,
        }) if cleanup_failure else patch.object(
            campaign, "_cleanup_uploaded_item", side_effect=REAL_CLEANUP_UPLOADED_ITEM))
        self.stack.enter_context(cleanup_patch)
        return stopped, waits

    def test_stop_during_recording_wait_cleans_confirmed_owned_media(self):
        stopped, waits = self._stop_while_third_account_waits()
        log = campaign.run_campaign(self.cfg, should_stop=stopped.is_set)
        video = Path(self.cfg.work_dir) / "fixture_native.mp4"
        self.assertEqual(log.status, "stopped")
        self.assertEqual(waits, ["recording_wait_tick"])
        self.assertEqual(self.send.call_count, 2)
        self.assertFalse(video.exists())
        self.assertFalse(video.with_name(video.name + ".managed.json").exists())

    def test_stop_cleanup_preserves_owned_media_referenced_by_pending_journal(self):
        stopped, _waits = self._stop_while_third_account_waits(pending_first=True)
        log = campaign.run_campaign(self.cfg, should_stop=stopped.is_set)
        video = Path(self.cfg.work_dir) / "fixture_native.mp4"
        self.assertEqual(log.status, "stopped")
        self.assertTrue(video.is_file())
        self.assertTrue(video.with_name(video.name + ".managed.json").is_file())
        self.assertTrue(upload._sidecar_path("pending-stop-fixture", 0).is_file())

    def test_stop_with_cleanup_disabled_retains_owned_media(self):
        stopped, _waits = self._stop_while_third_account_waits()
        self.cfg.cleanup_after_upload = False
        campaign.run_campaign(self.cfg, should_stop=stopped.is_set)
        video = Path(self.cfg.work_dir) / "fixture_native.mp4"
        self.assertTrue(video.is_file())
        self.assertTrue(video.with_name(video.name + ".managed.json").is_file())

    def test_stop_cleanup_failure_is_saved_and_emitted(self):
        stopped, _waits = self._stop_while_third_account_waits(cleanup_failure=True)
        events = []
        log = campaign.run_campaign(self.cfg, should_stop=stopped.is_set,
                                    progress=lambda kind, payload: events.append((kind, payload)))
        self.assertEqual(log.status, "stopped")
        self.assertTrue(any(issue.get("kind") == "storage_cleanup_error" for issue in log.issues))
        cleanup_event = next(payload for kind, payload in events if kind == "storage_cleanup")
        self.assertIn("PermissionError", cleanup_event["errors"][0])

    def test_cancelled_prefetch_is_joined_before_owned_temp_cleanup(self):
        current = Path(self.cfg.work_dir) / "current_native.mp4"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.write_bytes(b"current inert media")
        campaign._record_generated_media(current, root=Path(self.cfg.work_dir), role="native")
        next_path = Path(self.cfg.work_dir) / "next_native.mp4"
        entered, release = threading.Event(), threading.Event()
        clip = {"clip_uid": "next", "parent_video_uid": "next", "dur_s": 300,
                "source": "ego4d"}

        def prepare(_clip, _video, work_dir):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("fixture prefetch timed out")
            next_path.write_bytes(b"prefetched inert media")
            campaign._record_generated_media(next_path, root=Path(work_dir), role="native")
            return {"duration_ms": 300000, "video_path": str(next_path), "imu_real": True}

        prefetch = campaign._ClipPrefetch(Path(self.cfg.work_dir))
        cleanup_errors = []
        cleanup_results = []
        worker = None
        try:
            with patch.object(campaign, "prepare_clip", side_effect=prepare), \
                    patch.object(campaign, "_ego_clip_inputs", return_value=(clip, {})), \
                    patch.object(campaign, "_cleanup_uploaded_item",
                                 side_effect=REAL_CLEANUP_UPLOADED_ITEM), \
                    patch("moneymin.recovery.reconcile_confirmed", return_value=[]):
                prefetch.start(clip)
                self.assertTrue(entered.wait(3))

                def cleanup():
                    try:
                        cleanup_results.extend(campaign._cleanup_stopped_prepared_media(
                            {"video_path": str(current)}, {"clip_uid": "current", "source": "ego4d"},
                            Path(self.cfg.work_dir), prefetch))
                    except Exception as exc:
                        cleanup_errors.append(exc)

                worker = threading.Thread(target=cleanup)
                worker.start()
                self.assertTrue(current.is_file())
                self.assertTrue(next_path.is_file() is False)
                self.assertTrue(entered.is_set())
                # The cleanup worker must be blocked in shutdown until the
                # producer releases its output path.
                worker.join(0.05)
                self.assertTrue(worker.is_alive())
                self.assertTrue(current.is_file())
                release.set()
                worker.join(5)
        finally:
            release.set()
            if worker is not None:
                worker.join(5)
            prefetch.shutdown()
        self.assertFalse(worker.is_alive())
        self.assertEqual(cleanup_errors, [])
        self.assertEqual([row["files"] for row in cleanup_results], [2, 2], cleanup_results)
        self.assertFalse(current.exists())
        self.assertFalse(next_path.exists())

    def test_early_stop_between_clips_or_in_delay_cleans_retired_prefetch_only(self):
        for stop_point in ("between_clips", "delay"):
            with self.subTest(stop_point=stop_point):
                self.cfg.accounts = self.accounts[:1]
                self.cfg.account_workers = 1
                self.cfg.delay_mode = "fixed" if stop_point == "delay" else "off"
                self.cfg.delay_s = 1
                stopped = threading.Event()
                first = {"clip_uid": "one", "parent_video_uid": "one", "dur_s": 300,
                         "source": "ego4d"}
                following = {"clip_uid": "two", "parent_video_uid": "two", "dur_s": 300,
                             "source": "ego4d"}
                current_path = Path(self.cfg.work_dir) / "one_native.mp4"
                prefetched_path = Path(self.cfg.work_dir) / "two_native.mp4"
                produced = []

                def prepare(clip, _video, work_dir, **_kwargs):
                    path = (prefetched_path if clip["clip_uid"] == "two" else current_path)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(f"owned {clip['clip_uid']} fixture".encode())
                    campaign._record_generated_media(path, root=Path(work_dir), role="native")
                    produced.append(clip["clip_uid"])
                    return {"duration_ms": 300000, "video_path": str(path), "imu_real": True}

                prefetch = campaign._ClipPrefetch(Path(self.cfg.work_dir))
                try:
                    with patch.object(campaign, "prepare_clip", side_effect=prepare), \
                            patch.object(campaign, "_ego_clip_inputs", side_effect=lambda clip: (clip, {})):
                        prefetch.start(following)
                        self.assertEqual(prefetch._fut.result(timeout=5)["video_path"],
                                         str(prefetched_path))
                    prefetch.cancel()
                    self.assertEqual(prefetch.protected_paths(), set())
                    self.assertEqual(len(prefetch.cleanup_candidates()), 1,
                                     "completed retired output must remain discoverable")
                    self.cfg.candidate_plan = {self.task.task_id: [first, following]}
                    self.prepare.side_effect = prepare
                    self.send.side_effect = self.deliver
                    self.send.reset_mock()

                    def progress(kind, _payload):
                        if stop_point == "between_clips" and kind == "item_done":
                            stopped.set()

                    sleep_patch = (patch.object(campaign, "_interruptible_sleep",
                                                side_effect=lambda *_a, **_k: (
                                                    stopped.set() or True))
                                   if stop_point == "delay" else patch.object(
                                       campaign, "_interruptible_sleep", side_effect=lambda *_a, **_k: False))
                    with patch.object(campaign, "_ClipPrefetch", return_value=prefetch), \
                            patch.object(campaign, "_cleanup_uploaded_item",
                                         side_effect=REAL_CLEANUP_UPLOADED_ITEM), \
                            patch("moneymin.recovery.reconcile_confirmed", return_value=[]), \
                            sleep_patch:
                        log = campaign.run_campaign(self.cfg, should_stop=stopped.is_set,
                                                    progress=progress)
                finally:
                    prefetch.shutdown()

                self.assertEqual(log.status, "stopped")
                self.assertEqual(self.send.call_count, 1)
                self.assertEqual(produced, ["two", "one"])
                self.assertFalse(current_path.exists())
                self.assertFalse(prefetched_path.exists())


if __name__ == "__main__":
    unittest.main()
