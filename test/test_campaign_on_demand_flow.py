"""Bounded prepare/delivery/eviction scheduling, with inert local providers."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from moneymin import campaign
from moneymin.campaign_types import AccountSpec, CampaignConfig, TaskSpec


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


if __name__ == "__main__":
    unittest.main()
