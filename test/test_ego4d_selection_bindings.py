"""Inert declared catalogs; no downloads, accounts, authentication or media."""
import gzip
import hashlib
import json
import os
import pickle
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from moneymin import campaign, ego4d


class SelectionBindingTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.media = self.root / "ego4d"
        self.media.mkdir()
        self.meta = self.media / "ego4d.json"
        self.clips = self.media / "clips.csv"
        self.actions = self.media / "clip_narrations.json"
        self.timed = self.media / "timed_narrations.jsonl"
        self.seed = self.root / "seed.json.gz"
        self.task = "Folding Clothes or Putting Them on Hangers"
        self.video = {"video_uid": "parent", "duration_sec": 600, "has_imu": True,
                      "device": "AAAA", "scenarios": ["Cleaning / laundry"], "s3_path": "s3://fixture/parent.mp4",
                      "imu_metadata": {"s3_path": "s3://fixture/parent.csv", "component_metadata": [
                          {"canonical_video_start_ms": 0, "canonical_video_end_ms": 600000}]}}
        self.meta.write_text(json.dumps({"videos": [self.video]}), encoding="utf-8")
        self.clips.write_text("exported_clip_uid,parent_video_uid,parent_start_sec,parent_end_sec,s3_path\nc0,parent,0,600,s3://fixture/c0.mp4\n", encoding="utf-8")
        self.actions.write_text(json.dumps({"c0": "#C C folds the shirt"}), encoding="utf-8")
        self.events = [[float(t), "#C C folds the shirt"] for t in range(0, 601, 5)]
        self.timed.write_text(json.dumps({"video_uid": "parent", "events": self.events}) + "\n", encoding="utf-8")
        self.seed_payload = {"schema": 1, "tasks": {self.task: [{"clip_uid": "AAAA", "parent_video_uid": "parent", "s3_path": "s3://fixture/c0.mp4", "dur_s": 600, "window_s": [0, 600]}]}}
        self.seed.write_bytes(gzip.compress(json.dumps(self.seed_payload).encode(), mtime=0))
        for obj, field, value in ((ego4d, "EGO4D_DIR", self.media), (campaign.config, "MEDIA_DATA_DIR", self.root),
                                  (campaign.config, "DATA_DIR", self.root), (campaign, "_RANK_INPUT_SIGNATURE", None)):
            self.stack.enter_context(patch.object(obj, field, value))
        self.stack.enter_context(patch.object(ego4d, "sync_meta", return_value=(self.meta, self.clips)))
        self.stack.enter_context(patch.object(campaign, "_rank_seed_path", return_value=self.seed))
        for func in (ego4d._catalog, ego4d._action_index_cached, ego4d._load_timed_narrations_cached,
                     campaign._rank_cache_stamp, campaign._load_rank_seed, campaign._ranked_pools_cached, campaign._duration_ranked_pools):
            if hasattr(func, "cache_clear"):
                func.cache_clear()
                self.addCleanup(func.cache_clear)

    def same_stat_replace(self, path, old, new):
        raw = path.read_bytes()
        changed = raw.replace(old, new)
        self.assertNotEqual(raw, changed)
        self.assertEqual(len(raw), len(changed))
        info = path.stat()
        path.write_bytes(changed)
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))

    def test_catalog_same_stat_updates_parsed_metadata(self):
        self.assertEqual(ego4d._cat().videos["parent"]["device"], "AAAA")
        self.same_stat_replace(self.meta, b"AAAA", b"BBBB")
        self.assertEqual(ego4d._cat().videos["parent"]["device"], "BBBB")

    def test_actions_same_stat_updates_actual_parsed_text(self):
        self.assertIn("shirt", ego4d._action_index()["c0"])
        self.same_stat_replace(self.actions, b"shirt", b"tacos")
        self.assertIn("tacos", ego4d._action_index()["c0"])

    def test_timed_narrations_same_stat_updates_actual_events(self):
        self.assertIn("shirt", ego4d.load_timed_narrations()["parent"][0][1])
        self.same_stat_replace(self.timed, b"shirt", b"tacos")
        self.assertIn("tacos", ego4d.load_timed_narrations()["parent"][0][1])

    def test_seed_same_stat_replaces_memory_value(self):
        self.assertEqual(campaign._load_rank_seed()[self.task][0]["clip_uid"], "AAAA")
        info = self.seed.stat()
        changed = json.loads(json.dumps(self.seed_payload))
        changed["tasks"][self.task][0]["clip_uid"] = "BBBB"
        raw = gzip.compress(json.dumps(changed).encode(), mtime=0)
        self.assertEqual(len(raw), info.st_size)
        self.seed.write_bytes(raw)
        os.utime(self.seed, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertEqual(campaign._load_rank_seed()[self.task][0]["clip_uid"], "BBBB")

    def test_rank_memory_invalidates_same_stat_without_restart(self):
        with patch.object(campaign, "_ranked_pools_cached", return_value={}) as cached:
            campaign._ranked_pools()
            campaign._ranked_pools()
            self.assertEqual(cached.cache_clear.call_count, 1)
            self.same_stat_replace(self.meta, b"AAAA", b"BBBB")
            campaign._ranked_pools()
            self.assertEqual(cached.cache_clear.call_count, 2)

    def test_disk_rank_rejects_same_stat_changed_inputs(self):
        campaign._save_rank_cache({self.task: ()})
        self.assertIsNotNone(campaign._load_rank_cache())
        self.same_stat_replace(self.meta, b"AAAA", b"BBBB")
        self.assertIsNone(campaign._load_rank_cache())

    def test_duration_memory_rechecks_content(self):
        def current():
            return {self.task: ({"clip_uid": ego4d._cat().videos["parent"]["device"], "dur_s": 100},)}
        with patch.object(campaign, "_load_rank_cache", return_value=None), \
             patch.object(campaign, "_save_rank_cache"), \
             patch.object(campaign, "_ranked_pools_cached", side_effect=current):
            self.assertEqual(campaign._duration_ranked_pools(60, 120)[self.task][0]["clip_uid"], "AAAA")
            self.same_stat_replace(self.meta, b"AAAA", b"BBBB")
            self.assertEqual(campaign._duration_ranked_pools(60, 120)[self.task][0]["clip_uid"], "BBBB")

    def evidence_clip(self):
        helper = getattr(ego4d, "attach_selection_evidence", None)
        self.assertTrue(callable(helper), "selection carrier builder required")
        clip = ego4d.list_clips(min_dur_s=60, max_dur_s=1800)[0]
        return helper(clip, self.task)

    def test_evidence_hashes_exact_parsed_sources_and_no_paths(self):
        clip = self.evidence_clip()
        evidence = clip["selection_evidence"]
        self.assertEqual(evidence["schema"], 1)
        self.assertEqual(evidence["bindings"]["catalog"]["sha256"], hashlib.sha256(self.meta.read_bytes()).hexdigest())
        self.assertEqual(evidence["task"]["name"], self.task)
        self.assertEqual(evidence["candidate"]["window_s"], [0.0, 600.0])
        self.assertFalse(evidence["physical_provenance_verified"])
        self.assertNotIn(str(self.root), json.dumps(evidence))
        self.assertNotIn("s3://", json.dumps(evidence))
        validated = ego4d.revalidate_selection_evidence(clip, task_id="task-id", registry_key="registry")
        self.assertEqual(validated["task"]["id"], "task-id")
        self.assertEqual(validated["task"]["registry_key"], "registry")

    def test_source_and_candidate_and_task_changes_reject_before_effects(self):
        clip = self.evidence_clip()
        for field, value in (("window_s", [1, 600]), ("parent_video_uid", "other"), ("s3_path", "s3://fixture/other.mp4"),
                             ("dur_s", 599), ("source", "holoassist")):
            with self.subTest(field=field):
                changed = dict(clip, **{field: value})
                with self.assertRaises(ValueError):
                    ego4d.revalidate_selection_evidence(changed)
        with self.assertRaises(ValueError):
            ego4d.revalidate_selection_evidence(clip, task_name="Shopping")
        self.same_stat_replace(self.actions, b"shirt", b"tacos")
        with self.assertRaises(ValueError):
            ego4d.revalidate_selection_evidence(clip)

    def test_operation_reads_big_catalog_once_not_per_clip(self):
        boundary = getattr(ego4d, "selection_operation", None)
        self.assertTrue(callable(boundary), "snapshot boundary required")
        old_open = Path.open
        opens = []
        def opened(path, *args, **kwargs):
            if path == self.meta:
                opens.append(path)
            return old_open(path, *args, **kwargs)
        with patch.object(Path, "open", new=opened):
            with boundary():
                for _ in range(40):
                    self.assertEqual(ego4d._cat().videos["parent"]["device"], "AAAA")
        self.assertEqual(len(opens), 1)

    def test_operation_uses_hash_of_frozen_parsed_bytes_then_fresh_gate_rejects(self):
        boundary = getattr(ego4d, "selection_operation", None)
        self.assertTrue(callable(boundary))
        before = self.meta.read_bytes()
        with boundary():
            self.assertEqual(ego4d._cat().videos["parent"]["device"], "AAAA")
            self.same_stat_replace(self.meta, b"AAAA", b"BBBB")
            clip = self.evidence_clip()
            self.assertEqual(clip["selection_evidence"]["bindings"]["catalog"]["sha256"], hashlib.sha256(before).hexdigest())
        with self.assertRaises(ValueError):
            ego4d.revalidate_selection_evidence(clip)

    def test_fresh_gate_rejects_even_inside_previous_selection_operation(self):
        boundary = getattr(ego4d, "selection_operation", None)
        self.assertTrue(callable(boundary))
        with boundary():
            clip = self.evidence_clip()
            self.same_stat_replace(self.actions, b"shirt", b"tacos")
            with self.assertRaises(ValueError):
                ego4d.revalidate_selection_evidence(clip)

    def test_authoritative_task_identity_cannot_be_rebound(self):
        clip = self.evidence_clip()
        clip["selection_evidence"] = ego4d.revalidate_selection_evidence(
            clip, task_id="task-id", registry_key="registry")
        self.assertEqual(ego4d.revalidate_selection_evidence(
            clip, task_id="task-id", registry_key="registry")["task"]["id"], "task-id")
        with self.assertRaises(ValueError):
            ego4d.revalidate_selection_evidence(clip, task_id="different")
        with self.assertRaises(ValueError):
            ego4d.revalidate_selection_evidence(clip, registry_key="different")

    def test_direct_task_rank_outputs_persist_declared_source_evidence(self):
        result = ego4d.rank_all_task_spans(min_dur_s=60, max_dur_s=1800)
        self.assertTrue(result.get(self.task))
        for clip in result[self.task]:
            evidence = clip.get("selection_evidence")
            self.assertIsInstance(evidence, dict)
            self.assertEqual(evidence["task"]["name"], self.task)
            self.assertFalse(evidence["physical_provenance_verified"])
            self.assertEqual(evidence["bindings"]["timed"]["sha256"], hashlib.sha256(self.timed.read_bytes()).hexdigest())

    def test_persisted_rank_cache_contains_candidate_justification(self):
        clip = self.evidence_clip()
        campaign._save_rank_cache({self.task: (clip,)})
        cached = campaign._load_rank_cache()
        self.assertIsNotNone(cached)
        saved = cached[self.task][0]["selection_evidence"]
        self.assertEqual(saved["candidate"]["clip_uid"], "c0")
        self.assertEqual(saved["task"]["name"], self.task)
        self.assertEqual(saved["justification"]["status"], "declared_selection")
        self.assertEqual(saved["bindings"]["rank_seed"]["sha256"], hashlib.sha256(self.seed.read_bytes()).hexdigest())

    def test_missing_catalog_is_rejected_without_metadata_sync_or_download(self):
        clip = ego4d.list_clips(min_dur_s=60, max_dur_s=1800)[0]
        self.meta.unlink()
        self.clips.unlink()
        helper = getattr(ego4d, "attach_selection_evidence", None)
        self.assertTrue(callable(helper))
        clip = helper(clip, self.task)
        with patch.object(ego4d, "sync_meta", side_effect=AssertionError("effect gate must not sync/download")):
            with self.assertRaises(ValueError):
                ego4d.revalidate_selection_evidence(clip)

    def test_official_media_identity_and_offset_match_actual_prepare_plan(self):
        self.clips.write_text("exported_clip_uid,parent_video_uid,parent_start_sec,parent_end_sec,s3_path\nc0,parent,120,420,s3://fixture/c0.mp4\n", encoding="utf-8")
        clip = self.evidence_clip()
        runtime_clip, video = campaign._ego_clip_inputs(clip)
        self.assertEqual(video["video_uid"], "parent")
        plan = campaign._ego_prepare_plan(runtime_clip)
        candidate = clip["selection_evidence"]["candidate"]
        self.assertFalse(plan["needs_cut"])
        self.assertIsNone(plan["norm_start"])
        self.assertEqual(candidate["media_uid"], plan["clip_uid"])
        self.assertEqual(candidate["media_time_offset_s"], plan["window_s"][0] - float(plan["norm_start"] or 0))
