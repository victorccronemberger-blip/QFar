"""Finite cache contracts; byte fixtures are not playable or sensor evidence."""
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from moneymin import campaign


class NativeCacheIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "clip.mp4"
        self.output = self.root / "clip_native.mp4"
        self.marker = self.root / "clip_native.mp4.source.json"
        self.source.write_bytes(b"S" * (1024 * 1024 + 9))
        self.row = {"exported_clip_uid": "clip", "parent_video_uid": "parent",
                    "parent_start_sec": "0", "parent_end_sec": "60",
                    "needs_cut": False}
        self.commands = []
        self.output_bytes = b"N" * (1024 * 1024 + 11)
        for target, value in (("_ffmpeg_bin", "fixture-ffmpeg"), ("_use_nvenc", False)):
            mock = patch.object(campaign, target, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        for target, value in (("_cpu_slots", lambda *a: nullcontext()),
                              ("probe_video", lambda *a: {"duration_ms": 60000}),
                              ("_ffmpeg_run", self.encode)):
            mock = patch.object(campaign, target, side_effect=value)
            mock.start()
            self.addCleanup(mock.stop)

    def encode(self, command):
        self.commands.append(command)
        Path(command[-1]).write_bytes(self.output_bytes)
        return SimpleNamespace(returncode=0, stderr="")

    def fixture_marker(self):
        # Healthy fixtures use the advertised producer schema in each revision.
        # v4 has no output binding; v5 requires independently calculated bytes.
        result = campaign._native_cache_key(self.source, None, 60)
        if result["version"] >= 5:
            result.update(prepared_size=self.output.stat().st_size,
                          prepared_sha256=hashlib.sha256(self.output.read_bytes()).hexdigest())
        return result

    def prior(self):
        self.output.write_bytes(self.output_bytes)
        self.marker.write_text(json.dumps(self.fixture_marker()), encoding="utf-8")
        return self.output.read_bytes(), self.marker.read_bytes()

    def change_same_stat(self, path, byte):
        info = path.stat()
        path.write_bytes(byte * info.st_size)
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))

    def normalize(self, **kwargs):
        return campaign._normalize_video(self.source, self.root, dur_s=60, **kwargs)

    def cache_state(self):
        with patch.object(campaign, "_ego_clip_inputs", return_value=(self.row, {})), \
             patch.object(campaign.ego4d, "_valid_imu_cache", return_value=True):
            return campaign.ego_clip_cache_state(self.row, self.root)

    def assert_pair(self, before):
        self.assertTrue(self.output.is_file(), "prior video must survive")
        self.assertTrue(self.marker.is_file(), "prior marker must survive")
        self.assertEqual(self.output.read_bytes(), before[0])
        self.assertEqual(self.marker.read_bytes(), before[1])

    def test_source_hash_changes_even_when_stat_is_identical(self):
        before = campaign._native_cache_key(self.source, None, 60)
        self.change_same_stat(self.source, b"X")
        self.assertNotEqual(campaign._native_cache_key(self.source, None, 60), before)

    def test_unsupported_marker_rejected_without_media_io(self):
        markers = ({"version": 4}, {}, {"version": 5.0}, {"version": True}, None, [])
        with patch.object(campaign, "_native_cache_fingerprint",
                          side_effect=AssertionError("unexpected media hash")) as fingerprint, \
             patch.object(Path, "stat", side_effect=AssertionError("unexpected media stat")) as stat, \
             patch.object(Path, "open", side_effect=AssertionError("unexpected media open")) as opened:
            for marker in markers:
                with self.subTest(marker=marker):
                    self.assertFalse(campaign._native_cache_marker_matches(
                        marker, self.source, self.output, None, 60))
            fingerprint.assert_not_called()
            stat.assert_not_called()
            opened.assert_not_called()

    def test_supported_marker_rechecks_source_and_output_bytes(self):
        self.prior()
        saved = json.loads(self.marker.read_text(encoding="utf-8"))
        with patch.object(campaign, "_native_cache_fingerprint",
                          wraps=campaign._native_cache_fingerprint) as fingerprint:
            self.assertTrue(campaign._native_cache_marker_matches(
                saved, self.source, self.output, None, 60))
            self.assertTrue(any(call.args[0] == self.source for call in fingerprint.call_args_list))
            self.assertTrue(any(call.args[0] == self.output for call in fingerprint.call_args_list))
            fingerprint.reset_mock()
            self.change_same_stat(self.source, b"X")
            self.assertFalse(campaign._native_cache_marker_matches(
                saved, self.source, self.output, None, 60))
            self.assertTrue(any(call.args[0] == self.source for call in fingerprint.call_args_list))

    def test_source_changed_same_stat_is_not_ready(self):
        self.prior()
        self.change_same_stat(self.source, b"X")
        self.assertEqual(self.cache_state(), "partial")

    def test_output_changed_same_stat_is_not_ready(self):
        self.prior()
        self.change_same_stat(self.output, b"X")
        self.assertEqual(self.cache_state(), "partial")

    def test_source_changed_same_stat_forces_encode(self):
        self.prior()
        self.change_same_stat(self.source, b"X")
        self.normalize()
        self.assertEqual(len(self.commands), 1)

    def test_output_changed_same_stat_forces_encode(self):
        self.prior()
        self.change_same_stat(self.output, b"X")
        self.normalize()
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.output.read_bytes(), self.output_bytes)

    def test_encode_failures_preserve_old_pair(self):
        for failure in (RuntimeError("fixture"), TimeoutError("fixture"),
                        subprocess.TimeoutExpired("fixture", 1), OSError("fixture")):
            with self.subTest(failure=type(failure).__name__):
                before = self.prior()
                self.change_same_stat(self.source, b"X")
                with patch.object(campaign, "_ffmpeg_run", side_effect=failure):
                    with self.assertRaises(type(failure)):
                        self.normalize(start_s=1)
                self.assert_pair(before)

    def test_nonzero_encode_preserves_old_pair(self):
        before = self.prior()
        with patch.object(campaign, "_ffmpeg_run", return_value=SimpleNamespace(returncode=1, stderr="fixture")):
            with self.assertRaises(RuntimeError):
                self.normalize(start_s=1)
        self.assert_pair(before)

    def test_invalid_duration_preserves_old_pair(self):
        for duration in (0, -1, True, "bad", float("nan"), 120000):
            with self.subTest(duration=repr(duration)):
                before = self.prior()
                with patch.object(campaign, "probe_video", return_value={"duration_ms": duration}):
                    with self.assertRaises(RuntimeError):
                        self.normalize(start_s=1)
                self.assert_pair(before)

    def test_empty_candidate_preserves_old_pair(self):
        before = self.prior()
        self.output_bytes = b""
        with self.assertRaises(RuntimeError):
            self.normalize(start_s=1)
        self.assert_pair(before)

    def test_probe_exception_preserves_old_pair(self):
        before = self.prior()
        with patch.object(campaign, "probe_video", side_effect=ValueError("fixture")):
            with self.assertRaises(ValueError):
                self.normalize(start_s=1)
        self.assert_pair(before)

    def test_serialization_failure_preserves_old_pair(self):
        before = self.prior()
        with patch.object(campaign.json, "dumps", side_effect=TypeError("fixture")):
            with self.assertRaises(TypeError):
                self.normalize(start_s=1)
        self.assert_pair(before)

    def test_marker_write_failure_preserves_old_pair(self):
        before = self.prior()
        old_write = Path.write_text

        def write(path, *args, **kwargs):
            if ".source.json" in path.name:
                raise OSError("fixture marker write")
            return old_write(path, *args, **kwargs)

        with patch.object(Path, "write_text", new=write):
            with self.assertRaises(OSError):
                self.normalize(start_s=1)
        self.assert_pair(before)

    def test_marker_publish_failure_rolls_back_old_pair(self):
        before = self.prior()
        old_replace = Path.replace

        def replace(path, target):
            if Path(target) == self.marker:
                raise OSError("fixture marker publish")
            return old_replace(path, target)

        with patch.object(Path, "replace", new=replace):
            with self.assertRaises(OSError):
                self.normalize(start_s=1)
        self.assert_pair(before)

    def test_output_publish_failure_preserves_old_pair(self):
        before = self.prior()
        old_replace = Path.replace

        def replace(path, target):
            if Path(target) == self.output:
                raise OSError("fixture video publish")
            return old_replace(path, target)

        with patch.object(Path, "replace", new=replace):
            with self.assertRaises(OSError):
                self.normalize(start_s=1)
        self.assert_pair(before)

    def test_source_changes_during_encode_not_registered(self):
        before = self.prior()

        def encode(command):
            result = self.encode(command)
            self.change_same_stat(self.source, b"X")
            return result

        with patch.object(campaign, "_ffmpeg_run", side_effect=encode):
            with self.assertRaises(RuntimeError):
                self.normalize(start_s=1)
        self.assert_pair(before)

    def test_two_attempts_have_distinct_owned_video_temps(self):
        self.normalize(start_s=1)
        self.normalize(start_s=2)
        self.assertEqual(len(self.commands), 2)
        self.assertNotEqual(self.commands[0][-1], self.commands[1][-1])

    def test_same_path_threads_publish_one_valid_pair(self):
        first_entered = threading.Event()
        release = threading.Event()
        both_started = threading.Barrier(3)
        results, errors = [], []

        def encode(command):
            self.commands.append(command)
            if len(self.commands) == 1:
                first_entered.set()
                if not release.wait(3):
                    raise TimeoutError("fixture synchronization")
            Path(command[-1]).write_bytes(self.output_bytes)
            return SimpleNamespace(returncode=0, stderr="")

        def run():
            both_started.wait(3)
            try:
                results.append(self.normalize())
            except Exception as exc:
                errors.append(type(exc).__name__)

        with patch.object(campaign, "_ffmpeg_run", side_effect=encode):
            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            both_started.wait(3)
            self.assertTrue(first_entered.wait(3))
            time.sleep(0.15)
            release.set()
            for thread in threads:
                thread.join(4)
                self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.output.read_bytes(), self.output_bytes)
        self.assertEqual(self.cache_state(), "ready")

    def test_valid_cache_hit_does_not_encode(self):
        self.prior()
        self.assertEqual(self.normalize(), self.output)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.cache_state(), "ready")

    def test_success_registers_source_and_output_hashes(self):
        self.normalize()
        saved = json.loads(self.marker.read_text(encoding="utf-8"))
        self.assertEqual(saved.get("source_sha256"), hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(saved.get("prepared_sha256"), hashlib.sha256(self.output.read_bytes()).hexdigest())
        self.assertEqual(saved.get("prepared_size"), self.output.stat().st_size)
        self.assertEqual(self.cache_state(), "ready")

    def test_v4_failure_keeps_old_pair_and_success_migrates(self):
        self.output.write_bytes(self.output_bytes)
        v4 = campaign._native_cache_key(self.source, None, 60)
        v4.pop("source_sha256", None)
        v4["version"] = 4
        self.marker.write_text(json.dumps(v4), encoding="utf-8")
        before = self.output.read_bytes(), self.marker.read_bytes()
        with patch.object(campaign, "_ffmpeg_run", side_effect=TimeoutError("fixture")):
            with self.assertRaises(TimeoutError):
                self.normalize()
        self.assert_pair(before)
        self.normalize()
        self.assertEqual(json.loads(self.marker.read_text())["version"], 5)

    def test_first_failure_has_no_published_pair(self):
        with patch.object(campaign, "_ffmpeg_run", side_effect=TimeoutError("fixture")):
            with self.assertRaises(TimeoutError):
                self.normalize()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.marker.exists())

    def test_nvenc_fallback_keeps_cpu_codec_and_window(self):
        calls = []

        def encode(command):
            calls.append(command)
            Path(command[-1]).write_bytes(self.output_bytes)
            return SimpleNamespace(returncode=int(len(calls) == 1), stderr="fixture")

        with patch.object(campaign, "_use_nvenc", return_value=True), \
             patch.object(campaign, "_ffmpeg_run", side_effect=encode):
            self.normalize(start_s=7)
        self.assertEqual(len(calls), 2)
        self.assertIn("h264_nvenc", calls[0])
        self.assertIn("libx264", calls[1])
        self.assertEqual(calls[0][-1], calls[1][-1])
        self.assertEqual(calls[1][calls[1].index("-t") + 1], "60.000")
        self.assertEqual(calls[1][calls[1].index("-ss") + 1], "5.000")

    def test_unrelated_temporary_file_not_deleted(self):
        unrelated = self.root / "clip_native.tmp.mp4"
        unrelated.write_bytes(b"other writer")
        self.normalize()
        self.assertEqual(unrelated.read_bytes(), b"other writer")

    def test_owned_temps_cleaned_after_failure(self):
        before = self.prior()

        def encode(command):
            Path(command[-1]).write_bytes(b"partial")
            raise TimeoutError("fixture")

        with patch.object(campaign, "_ffmpeg_run", side_effect=encode):
            with self.assertRaises(TimeoutError):
                self.normalize(start_s=1)
        self.assert_pair(before)
        self.assertEqual({p.name for p in self.root.iterdir()},
                         {self.source.name, self.output.name, self.marker.name})

    def test_empty_prior_pair_retains_original_publish_error(self):
        self.output.write_bytes(b"")
        self.marker.write_bytes(b"")
        old_replace = Path.replace

        def replace(path, target):
            if Path(target) == self.marker:
                raise OSError("fixture original marker failure")
            return old_replace(path, target)

        with patch.object(Path, "replace", new=replace):
            with self.assertRaisesRegex(OSError, "fixture original marker failure"):
                self.normalize()
        self.assert_pair((b"", b""))

    def test_second_backup_failure_preserves_pair_and_cleans_first(self):
        before = self.prior()
        old_link = os.link
        calls = []

        def link(source, target):
            calls.append(source)
            if len(calls) == 2:
                raise OSError("fixture backup failure")
            return old_link(source, target)

        with patch.object(campaign.os, "link", new=link):
            with self.assertRaisesRegex(OSError, "fixture backup failure"):
                self.normalize(start_s=1)
        self.assert_pair(before)
        self.assertEqual({p.name for p in self.root.iterdir()},
                         {self.source.name, self.output.name, self.marker.name})

    def test_source_changed_during_backup_not_published(self):
        before = self.prior()
        old_link = os.link

        def link(source, target):
            result = old_link(source, target)
            self.change_same_stat(self.source, b"X")
            return result

        with patch.object(campaign.os, "link", new=link):
            with self.assertRaises(RuntimeError):
                self.normalize(start_s=1)
        self.assert_pair(before)
