"""Local declared catalog/sensors; no accounts, provider downloads or uploads."""
from contextlib import ExitStack
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from moneymin import campaign, content_provenance, ego4d, ego_accelerator, imu_coverage
from moneymin.campaign_types import AccountSpec


class MediaSelectionRepairTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.catalog = self.root / "ego4d"
        self.catalog.mkdir()
        self.task = "Folding Clothes or Putting Them on Hangers"
        self.video = {"video_uid": "parent", "duration_sec": 600, "has_imu": True,
                      "scenarios": ["Cleaning / laundry"], "s3_path": "s3://fixture/parent.mp4"}
        self.meta = self.catalog / "ego4d.json"
        self.clips = self.catalog / "clips.csv"
        self.actions = self.catalog / "clip_narrations.json"
        self.timed = self.catalog / "timed_narrations.jsonl"
        self.meta.write_text(json.dumps({"videos": [self.video]}), encoding="utf-8")
        self.clips.write_text("exported_clip_uid,parent_video_uid,parent_start_sec,parent_end_sec,s3_path\n"
                              "official,parent,0,600,s3://fixture/official.mp4\n", encoding="utf-8")
        self.actions.write_text(json.dumps({"official": "#C C folds the shirt"}), encoding="utf-8")
        self.timed.write_text(json.dumps({"video_uid": "parent", "events": [
            [float(t), "#C C folds the shirt"] for t in range(0, 601, 5)]}) + "\n", encoding="utf-8")
        for obj, key, value in ((ego4d, "EGO4D_DIR", self.catalog),
                                (campaign.config, "DATA_DIR", self.root),
                                (campaign.config, "MEDIA_DATA_DIR", self.root)):
            self.stack.enter_context(patch.object(obj, key, value))
        self.stack.enter_context(patch.object(ego4d, "sync_meta", return_value=(self.meta, self.clips)))
        self.no_download = self.stack.enter_context(patch.object(
            ego4d, "_download_to", side_effect=AssertionError("provider download forbidden")))
        self.sensor = self.root / "parent_imu.csv"
        with self.sensor.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(["canonical_timestamp_ms", "gyro_x", "gyro_y", "gyro_z", "accl_x", "accl_y", "accl_z"])
            for timestamp in range(0, 600001, 10):
                if not 200000 < timestamp < 201000:
                    writer.writerow([timestamp, 1, 2, 3, 4, 5, 6])

    def original(self):
        raw = ego4d.list_clips(min_dur_s=60, max_dur_s=1800)[0]
        return ego4d.attach_selection_evidence(raw, self.task, task_id="fold-task", registry_key="fold-registry")

    def assert_current_gate(self, clip):
        evidence = ego4d.revalidate_selection_evidence(
            clip, task_name=self.task, task_id="fold-task", registry_key="fold-registry")
        self.assertEqual(evidence["candidate"]["window_s"], list(clip["window_s"]))
        self.assertEqual(evidence["task"]["id"], "fold-task")
        self.assertEqual(evidence["task"]["registry_key"], "fold-registry")
        self.assertTrue(campaign._prepare_queue_accepts(clip, self.task))
        ego4d.build_imu_csv(self.sensor, clip["window_s"], validate_only=True)

    def test_refinement_aligns_sensor_subwindows_with_current_task_evidence(self):
        original = self.original()
        result = imu_coverage.refine_candidates([original], self.root, 60, 600)
        self.assertEqual([clip["window_s"] for clip in result], [(5.0, 195.0), (205.0, 595.0)])
        for clip in result:
            self.assert_current_gate(clip)
            self.assertEqual(clip["dedup_clip_uids"], ["official"])
            self.assertEqual(clip["imu_refined_from"], "official")
            self.assertEqual(clip["media_uid"], "official")
        self.no_download.assert_not_called()

    def test_carve_selects_longest_proved_continuous_subwindow(self):
        carved = imu_coverage.carve_continuous_window(self.original(), self.root, min_s=60, max_s=600)
        self.assertEqual(carved["window_s"], (205.0, 595.0))
        self.assertEqual(carved["imu_carved_from"], "official")
        self.assert_current_gate(carved)
        self.no_download.assert_not_called()

    def test_carve_keeps_current_task_proof_inside_a_shorter_chosen_maximum(self):
        carved = imu_coverage.carve_continuous_window(self.original(), self.root, min_s=60, max_s=240)
        self.assertEqual(carved["window_s"], (205.0, 440.0))
        self.assert_current_gate(carved)

    def test_changed_task_evidence_cannot_be_reissued_after_a_sensor_cut(self):
        original = self.original()
        self.actions.write_text(json.dumps({"official": "#C C plays basketball"}), encoding="utf-8")
        self.timed.write_text(json.dumps({"video_uid": "parent", "events": [
            [float(t), "#C C plays basketball"] for t in range(0, 601, 5)]}) + "\n", encoding="utf-8")
        self.assertEqual(imu_coverage.refine_candidates([original], self.root, 60, 600), [])
        self.assertIsNone(imu_coverage.carve_continuous_window(original, self.root, min_s=60, max_s=600))

    def test_ready_scenario_expansion_gets_a_carrier_accepted_at_the_upload_gate(self):
        raw = ego4d.list_clips(min_dur_s=60, max_dur_s=1800)[0]
        buckets = ego_accelerator.assign_scenario_clips([raw])
        with patch.object(ego_accelerator, "scenario_buckets", return_value=buckets), \
             patch.object(campaign, "ego_clip_cache_state", return_value="ready"):
            selected = ego_accelerator.ready_scenario_clips(self.task, allow_disabled=True, work_dir=self.root)
        self.assertEqual(len(selected), 1)
        clip = selected[0]
        self.assertTrue(campaign._prepare_queue_accepts(clip, self.task))
        # An inert byte fixture proves the real lineage/selection gate reaches
        # the mocked account boundary; it is never sent or decoded.
        media = self.root / "inert.mp4"
        media.write_bytes(b"Declared inert media fixture")
        frames = "i,ptsNs,dtNs,tNs,key\n0,0,0,0,1\n"
        imu = "t,ax,ay,az,wx,wy,wz\n0,1,2,3,4,5,6\n"
        item = {"source": "ego4d", "imu_real": True, "duration_ms": 600000,
                "video_path": str(media), "imu_csv": imu, "frames_csv": frames,
                "clip_uid": clip["clip_uid"], "_content_candidate": clip,
                "task_name_authoritative": self.task}
        item.update(content_provenance.prepare_content_provenance(
            media, self.sensor, media, imu, frames, clip_uid=clip["clip_uid"],
            parent_video_uid="parent", media_uid="official", window_s=(0, 600),
            media_offset_s=0, normalization_start_s=None, selection_evidence=clip["selection_evidence"]))
        with patch.object(campaign.org_policy, "account_kind", return_value="other"), \
             patch.object(campaign.Session, "from_email", side_effect=AssertionError("inert account boundary reached")) as create, \
             patch.object(campaign, "upload_session") as send:
            with self.assertRaisesRegex(AssertionError, "inert account boundary reached"):
                campaign.upload_to_account(item, AccountSpec("fixture@example.invalid", "fixture-org"),
                                           "fold-task", 30, True, True, recover_pending=False)
        create.assert_called_once()
        send.assert_not_called()

    def test_ready_scenario_cannot_substitute_scenario_for_missing_action_evidence(self):
        raw = ego4d.list_clips(min_dur_s=60, max_dur_s=1800)[0]
        buckets = ego_accelerator.assign_scenario_clips([raw])
        self.actions.write_text("{}", encoding="utf-8")
        self.timed.write_text("", encoding="utf-8")
        with patch.object(ego_accelerator, "scenario_buckets", return_value=buckets), \
             patch.object(campaign, "ego_clip_cache_state", return_value="ready"):
            self.assertEqual(ego_accelerator.ready_scenario_clips(self.task, allow_disabled=True), [])
        self.no_download.assert_not_called()

    def test_scenario_query_binds_catalog_once_for_multiple_current_carriers(self):
        ids = ["c0", "c1", "c2"]
        self.clips.write_text("exported_clip_uid,parent_video_uid,parent_start_sec,parent_end_sec,s3_path\n" +
            "".join(f"{uid},parent,0,600,s3://fixture/{uid}.mp4\n" for uid in ids), encoding="utf-8")
        self.actions.write_text(json.dumps({uid: "#C C folds the shirt" for uid in ids}), encoding="utf-8")
        buckets = ego_accelerator.assign_scenario_clips(ego4d.list_clips(min_dur_s=60, max_dur_s=1800))
        old_open = Path.open
        reads = []
        def opened(path, *args, **kwargs):
            if path == self.meta:
                reads.append(path)
            return old_open(path, *args, **kwargs)
        with patch.object(ego_accelerator, "scenario_buckets", return_value=buckets), \
             patch.object(campaign, "ego_clip_cache_state", return_value="ready"), \
             patch.object(Path, "open", new=opened):
            selected = ego_accelerator.ready_scenario_clips(self.task, allow_disabled=True)
        self.assertEqual(len(selected), 3)
        self.assertEqual(len(reads), 1)


class MirrorSourceIdentityTests(unittest.TestCase):
    def test_generated_window_retries_the_real_export_uid(self):
        clip = {"clip_uid": "parent_120.000_240.000", "exported_clip_uid": "parent_120.000_240.000",
                "parent_video_uid": "parent", "media_uid": "official-export", "needs_cut": False,
                "s3_path": "s3://ego4d-bristol/public/v1/clips/official-export.mp4"}
        attempts = []
        def download(bucket, key, dest, **kwargs):
            attempts.append((bucket, key))
            if bucket == "ego4d-speac" and key == "public/v2/clips/official-export.mp4":
                Path(dest).write_bytes(b"Declared inert source bytes")
                return Path(dest)
            raise OSError("declared primary unavailable")
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(ego4d, "_valid_mp4_cache", return_value=False), \
             patch.object(ego4d, "_download_to", side_effect=download):
            destination = Path(folder) / "official-export.mp4"
            self.assertEqual(ego4d.download_clip(clip, destination), destination)
        self.assertEqual(attempts, [("ego4d-bristol", "public/v1/clips/official-export.mp4"),
                                    ("ego4d-speac", "public/v2/clips/official-export.mp4")])

    def test_full_parent_sources_do_not_try_generated_clip_mirrors(self):
        self.assertEqual(ego4d._clip_s3_candidates({"clip_uid": "parent_120_240", "media_uid": "parent",
            "parent_video_uid": "parent", "s3_path": "s3://fixture/full_scale/parent.mp4"}),
            [("fixture", "full_scale/parent.mp4")])
