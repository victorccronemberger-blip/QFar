from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from moneymin import nymeria_library as library

FOLD = "Folding Clothes or Putting Them on Hangers"


class Response:
    def __init__(self, data, status=200, headers=None):
        self.data = data
        self.status_code = status
        self.headers = {"Content-Length": str(len(data)), **(headers or {})}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, _size):
        midpoint = len(self.data) // 2
        yield self.data[:midpoint]
        yield self.data[midpoint:]


class Session:
    def __init__(self, handler):
        self.handler = handler

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get(self, url, **kwargs):
        return self.handler(url, kwargs)


class NymeriaLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = (Path(self.temporary.name) / "library").resolve()
        self.manifest = Path(self.temporary.name) / "manifest.json"
        self.payloads = {}
        self.catalog = {"sequences": {}}
        self.calls = []
        self.add_sequence("sequence_one")
        self.publish()
        self.patcher = patch.object(library.requests, "Session", side_effect=lambda: Session(self.respond))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def asset(self, filename, data):
        url = "https://scontent.xx.fbcdn.net/" + filename
        self.payloads[url] = data
        return {"filename": filename, "sha1sum": hashlib.sha1(data).hexdigest(),
                "file_size_bytes": len(data), "download_url": url}

    def archive(self, files):
        handle = io.BytesIO()
        with zipfile.ZipFile(handle, "w") as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        return handle.getvalue()

    def add_sequence(self, sid, *, empty=False):
        text = "start_time,end_time,Describe my atomic actions\n"
        for index in range(91):
            text += f"{1000 + index * 5},{1005 + index * 5},C is folding a shirt with both hands.\n"
        annotations = {} if empty else {"narration/atomic_action.csv": text}
        self.catalog["sequences"][sid] = {
            "metadata_json": self.asset(sid + "_metadata.json", json.dumps({
                "uid": sid, "script": "S11-Laundary", "head_duration_sec": 455}).encode()),
            "narration": self.asset(sid + "_narration.zip", self.archive(annotations)),
            "timesync_and_imu": self.asset(sid + "_imu.zip", self.archive({
                "recording_head/data/motion.vrs": b"real-source-imu-placeholder",
                "recording_rwrist/data/motion.vrs": b"unselected-wrist"})),
            "recording_head_data_data_vrs": self.asset(sid + "_data.vrs", b"real-source-video-placeholder")}

    def publish(self):
        self.manifest.write_text(json.dumps(self.catalog), "utf8")
        library.import_manifest(self.manifest, self.root)

    def respond(self, url, kwargs):
        self.calls.append((url, kwargs))
        data = self.payloads[url]
        if "Range" in kwargs["headers"]:
            start, raw_end = kwargs["headers"]["Range"].split("=")[1].split("-")
            offset = int(start)
            end = int(raw_end) if raw_end else len(data) - 1
            return Response(data[offset:end + 1], 206, {"Content-Range": f"bytes {offset}-{end}/{len(data)}"})
        return Response(data)

    def test_entire_catalog_is_imported_and_annotation_only_never_ready(self):
        self.add_sequence("sequence_two")
        self.add_sequence("sequence_empty", empty=True)
        self.publish()
        result = library.sync_catalog(self.root, max_workers=3)
        self.assertEqual(result["sequence_count"], 3)
        self.assertEqual(result["by_state"]["cataloged"], 2)
        self.assertEqual(result["by_state"]["no_annotations"], 1)
        self.assertEqual(len(self.calls), 6)
        self.assertTrue(all(not row["selection_ready"] for row in library.inventory(self.root)["items"]))
        self.assertFalse((self.root / "sequence_one/recording_head/data/data.vrs").exists())
        self.assertNotIn("download_url", json.dumps(library.inventory(self.root)))

    def test_catalog_only_campaign_lookup_skips_sdk_and_csv_parsing(self):
        library.sync_catalog(self.root)
        with patch.object(library.nymeria, "_annotation_rows", side_effect=AssertionError("catalog source is not media")), \
                patch.object(library.nymeria, "_sdk_signature", side_effect=AssertionError("catalog source needs no SDK hash")):
            self.assertEqual(library.nymeria.automatic_candidates(task_name=FOLD, root=self.root), [])

    def test_annotation_plan_finds_full_universe_and_requires_real_clocks(self):
        self.add_sequence("sequence_two")
        self.publish()
        library.sync_catalog(self.root)
        result = library.plan_expansion([FOLD], target_seconds=800, root=self.root)
        self.assertEqual(result["sequence_count"], 2)
        self.assertGreaterEqual(result["potential_seconds"], 800)
        self.assertEqual(set(result["selected_seq_ids"]), {"sequence_one", "sequence_two"})
        self.assertFalse(result["campaign_ready"])
        self.assertIn("requires_measured", result["capacity_kind"])

    def test_download_pause_retains_bound_partial_and_resume_verifies_sha1(self):
        asset = self.asset("resume.vrs", b"12345678")
        target = self.root / "resume.vrs"
        calls = 0

        def stop():
            nonlocal calls
            calls += 1
            return calls == 3

        with self.assertRaises(library.NymeriaCancelled):
            library._download(asset, target, self.root, should_stop=stop)
        self.assertEqual(target.with_name("resume.vrs.part").read_bytes(), b"1234")
        self.assertFalse(target.exists())
        library._download(asset, target, self.root)
        self.assertEqual(target.read_bytes(), b"12345678")
        self.assertEqual(self.calls[-1][1]["headers"]["Range"], "bytes=4-")
        self.assertFalse(target.with_name("resume.vrs.part.binding.json").exists())

    def test_bad_hash_bad_range_and_existing_source_are_preserved(self):
        asset = self.asset("bad.vrs", b"12345678")
        asset["sha1sum"] = "a" * 40
        target = self.root / "bad.vrs"
        with self.assertRaises(ValueError):
            library._download(asset, target, self.root)
        self.assertTrue(target.with_name("bad.vrs.part").exists())
        self.assertFalse(target.exists())
        target.write_bytes(b"user-owned-file")
        with self.assertRaises(ValueError):
            library._download(asset, target, self.root)
        self.assertEqual(target.read_bytes(), b"user-owned-file")

    def test_download_sequence_uses_original_sources_then_real_sdk_gate(self):
        snap = {"duration_s": 455, "measured": {"imu_sample_count": 227500},
                "proof": {"sdk_signature": [[name, list(marker)]
                    for name, marker in library.nymeria._sdk_signature() if isinstance(marker, tuple)]}}
        with patch.object(library.nymeria, "_snapshot", return_value=snap) as measured:
            result = library.acquire_sequences(["sequence_one"], self.root, min_free_bytes=0)
        self.assertTrue(result["ok"])
        measured.assert_called_once_with(self.root / "sequence_one", fresh=True)
        self.assertEqual((self.root / "sequence_one/recording_head/data/motion.vrs").read_bytes(),
                         b"real-source-imu-placeholder")
        self.assertFalse((self.root / "sequence_one/recording_rwrist").exists())
        item = library.inventory(self.root)["items"][0]
        self.assertEqual(item["state"], "measured")
        self.assertTrue(item["selection_ready"])
        (self.root / "sequence_one/recording_head/data/data.vrs").write_bytes(b"changed-source")
        self.assertFalse(library.inventory(self.root)["items"][0]["selection_ready"])

    def test_source_sdk_rejection_never_marks_recording_measured(self):
        progress = []
        with patch.object(library.nymeria, "_snapshot", side_effect=ValueError("clock gap")), \
                self.assertRaises(library.NymeriaLibraryError) as raised:
            library.acquire_sequences(["sequence_one"], self.root, min_free_bytes=0, progress=progress.append)
        self.assertEqual(raised.exception.code, "source_measurement_invalid")
        self.assertEqual(progress[-1]["sequence_id"], "sequence_one")
        self.assertEqual(library.inventory(self.root)["items"][0]["state"], "downloaded")
        self.assertFalse(library.inventory(self.root)["items"][0]["selection_ready"])

    def test_invalid_manifest_and_zip_escape_do_not_write_outside_library(self):
        original = (self.root / "_catalog/download_urls.json").read_bytes()
        self.catalog["sequences"]["../escape"] = self.catalog["sequences"]["sequence_one"]
        self.manifest.write_text(json.dumps(self.catalog), "utf8")
        with self.assertRaises(ValueError):
            library.import_manifest(self.manifest, self.root)
        self.assertEqual((self.root / "_catalog/download_urls.json").read_bytes(), original)
        archive = Path(self.temporary.name) / "escape.zip"
        archive.write_bytes(self.archive({"narration/../../escape.csv": b"escape"}))
        with self.assertRaises(ValueError):
            library._extract(archive, self.root, "sequence_one", "narration/")
        self.assertFalse((self.root.parent / "escape.csv").exists())

    def test_low_disk_fails_before_any_large_download(self):
        library.sync_catalog(self.root)
        self.calls.clear()
        with patch.object(library.shutil, "disk_usage", return_value=type("Disk", (), {"free": 1})()), \
                self.assertRaises(library.NymeriaLibraryError):
            library.acquire_sequences(["sequence_one"], self.root, min_free_bytes=0)
        self.assertEqual(self.calls, [])

    def test_cached_inventory_reuses_annotations_and_invalidates_changed_sources(self):
        library.sync_catalog(self.root)
        expected = library.summary(self.root)["atomic_action_count"]
        with patch.object(library.nymeria, "_annotation_rows", side_effect=AssertionError("warm inventory must reuse parsed rows")):
            self.assertEqual(library.summary(self.root)["atomic_action_count"], expected)
            self.assertEqual(library.inventory(self.root)["items"][0]["atomic_action_count"], expected)
        (self.root / "sequence_one/narration/atomic_action.csv").write_text(
            "start_time,end_time,Describe my atomic actions\n1000,1005,C is holding a shirt.\n", "utf8")
        self.assertEqual(library.summary(self.root)["atomic_action_count"], 1)

    def test_plan_can_pause_without_erasing_catalog_and_duplicate_tasks_do_not_multiply_capacity(self):
        library.sync_catalog(self.root)
        with self.assertRaises(library.NymeriaCancelled):
            library.plan_expansion([FOLD], root=self.root, should_stop=lambda: True)
        self.assertTrue((self.root / "sequence_one/metadata.json").exists())
        first = library.plan_expansion([FOLD], root=self.root)
        repeated = library.plan_expansion([FOLD, FOLD], root=self.root)
        self.assertEqual(first["potential_seconds"], repeated["potential_seconds"])

    def test_plan_disk_fit_includes_extraction_and_reserved_free_space(self):
        library.sync_catalog(self.root)
        self.assertIsNone(library.plan_expansion([FOLD], root=self.root)["fits_available_disk"])
        library._extraction_bytes(self.root, "sequence_one", self.catalog["sequences"]["sequence_one"], fetch=True)
        first = library.plan_expansion([FOLD], root=self.root, min_free_bytes=0)
        available = first["download_bytes"] + first["working_bytes"]
        with patch.object(library.shutil, "disk_usage", return_value=type("Disk", (), {"free": available})()):
            self.assertTrue(library.plan_expansion([FOLD], root=self.root, min_free_bytes=0)["fits_available_disk"])
            self.assertFalse(library.plan_expansion([FOLD], root=self.root, min_free_bytes=1)["fits_available_disk"])

    def test_batch_space_quote_counts_all_extracted_motion_streams_before_gigabytes(self):
        self.add_sequence("sequence_two")
        self.publish()
        library.sync_catalog(self.root)
        ids = ["sequence_one", "sequence_two"]
        sources = sum(library._sequence_item(self.root, sid, self.catalog["sequences"][sid])["download_bytes"] for sid in ids)
        head = len(b"real-source-imu-placeholder")
        # Enough for both compressed assets and one extracted IMU, but not two.
        with patch.object(library.shutil, "disk_usage", return_value=type("Disk", (), {"free": sources + head})()), \
                self.assertRaises(library.NymeriaLibraryError) as raised:
            library.acquire_sequences(ids, self.root, min_free_bytes=0)
        self.assertEqual(raised.exception.code, "extraction_space_insufficient")
        self.assertFalse((self.root / "sequence_one/recording_head/data/data.vrs").exists())
        self.assertFalse((self.root / "_catalog/archives/sequence_one/timesync_and_imu.zip").exists())

    def test_invalid_remote_zip_index_exposes_safe_failure_without_starting_large_asset(self):
        library.sync_catalog(self.root)
        with patch.object(library.requests, "Session", side_effect=lambda: Session(
                lambda _url, _kwargs: Response(b"bad range", status=200))), \
                self.assertRaises(library.NymeriaLibraryError) as raised:
            library.acquire_sequences(["sequence_one"], self.root, min_free_bytes=0)
        self.assertEqual(raised.exception.code, "zip_index_range_invalid")
        self.assertNotIn("https://", raised.exception.safe_message)
        self.assertFalse((self.root / "sequence_one/recording_head/data/data.vrs").exists())


if __name__ == "__main__":
    unittest.main()
