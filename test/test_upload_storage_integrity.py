"""Migration preserves both copies when a preexisting archive conflicts."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moneymin import config, upload_storage


class UploadArchiveMigrationIntegrityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.user = self.root / "user"
        self.legacy = self.root / "library" / "sidecars"
        self.destination = self.user / "sidecars"
        self.tokens = self.root / "tokens"
        for directory in (self.legacy, self.destination, self.tokens):
            directory.mkdir(parents=True)
        (self.tokens / "token_fake.json").write_text(json.dumps({"email": "one@example.com"}))
        self.row = {"session_id": "session1", "account_email": "one@example.com", "chunk_index": 0}
        (self.legacy / "session1.json").write_text(json.dumps(self.row))
        self.source = self.legacy / "session1.data.zip"
        self.target = self.destination / "session1.data.zip"
        self.source.write_bytes(b"source-archive")
        for mocked in (patch.object(config, "DATA_DIR", self.user),
                       patch.object(config, "MEDIA_DATA_DIR", self.legacy.parent),
                       patch.object(config, "tokens_dir", return_value=self.tokens),
                       patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))):
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_conflicting_orphan_archive_is_preserved_without_acknowledgment(self):
        self.target.write_bytes(b"different-existing-archive")
        for attempt in range(2):
            with self.subTest(attempt=attempt), self.assertRaises(ValueError):
                upload_storage.journal_directory()
        self.assertEqual(self.target.read_bytes(), b"different-existing-archive")
        self.assertEqual(self.source.read_bytes(), b"source-archive")
        self.assertFalse((self.destination / "session1.json").exists())
        self.assertFalse((self.user / "sidecar_migration.json").exists())

    def test_identical_orphan_archive_is_reused_without_rewrite(self):
        self.target.write_bytes(self.source.read_bytes())
        metadata_before = self.target.stat().st_mtime_ns
        original_open = Path.open

        def no_archive_rewrite(path, mode="r", *args, **kwargs):
            if path == self.target and any(flag in mode for flag in ("w", "a", "+")):
                raise AssertionError("existing archive must not be rewritten")
            return original_open(path, mode, *args, **kwargs)

        with patch.object(Path, "open", no_archive_rewrite):
            directory = upload_storage.journal_directory()
        self.assertEqual(directory, self.destination)
        self.assertEqual(self.target.stat().st_mtime_ns, metadata_before)
        row = json.loads((self.destination / "session1.json").read_text())
        self.assertEqual(Path(row["sidecar_data_path"]), self.target.resolve())
        marker = json.loads((self.user / "sidecar_migration.json").read_text())
        self.assertIn("session1.json", marker[str(self.legacy.resolve())])

    def test_concurrent_archive_creation_cannot_be_replaced(self):
        original_open = Path.open

        def create_before_exclusive_open(path, mode="r", *args, **kwargs):
            if path == self.target and mode == "xb":
                with original_open(path, "wb") as stream:
                    stream.write(b"other-writer-archive")
            return original_open(path, mode, *args, **kwargs)

        with patch.object(Path, "open", create_before_exclusive_open), self.assertRaises(ValueError):
            upload_storage.journal_directory()
        self.assertEqual(self.target.read_bytes(), b"other-writer-archive")
        self.assertFalse((self.destination / "session1.json").exists())
        self.assertFalse((self.user / "sidecar_migration.json").exists())

    def test_interrupted_journal_write_leaves_archive_for_retry(self):
        with patch.object(upload_storage, "save_json", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                upload_storage.journal_directory()
        self.assertEqual(self.target.read_bytes(), self.source.read_bytes())
        self.assertFalse((self.user / "sidecar_migration.json").exists())
        upload_storage.journal_directory()
        self.assertTrue((self.destination / "session1.json").exists())

    def migrated_row(self):
        return {**self.row, "sidecar_data_path": str(self.target.resolve())}

    def test_conflicting_existing_journal_is_preserved_without_acknowledgment(self):
        journal = self.destination / "session1.json"
        marker = self.user / "sidecar_migration.json"
        marker_text = json.dumps({str(self.legacy.resolve()): ["previous.json"]})
        marker.write_text(marker_text)
        for content in ("invalid-json", json.dumps({**self.migrated_row(), "account_email": "other@example.com"}),
                        json.dumps({**self.migrated_row(), "session_id": "other-session"}),
                        json.dumps({**self.migrated_row(), "state": "done"})):
            journal.write_text(content)
            with self.subTest(content=content), self.assertRaises(ValueError):
                upload_storage.journal_directory()
            self.assertEqual(journal.read_text(), content)
            self.assertEqual(marker.read_text(), marker_text)
            self.assertEqual(self.source.read_bytes(), b"source-archive")
            self.assertFalse(self.target.exists())

    def test_identical_existing_journal_and_archive_are_reused(self):
        journal = self.destination / "session1.json"
        content = json.dumps(self.migrated_row(), sort_keys=True)
        journal.write_text(content)
        self.target.write_bytes(self.source.read_bytes())
        journal_metadata = journal.stat().st_mtime_ns
        archive_metadata = self.target.stat().st_mtime_ns
        upload_storage.journal_directory()
        self.assertEqual(journal.read_text(), content)
        self.assertEqual(journal.stat().st_mtime_ns, journal_metadata)
        self.assertEqual(self.target.stat().st_mtime_ns, archive_metadata)
        marker = json.loads((self.user / "sidecar_migration.json").read_text())
        self.assertIn("session1.json", marker[str(self.legacy.resolve())])

    def test_matching_journal_cannot_acknowledge_conflicting_archive(self):
        journal = self.destination / "session1.json"
        journal.write_text(json.dumps(self.migrated_row()))
        self.target.write_bytes(b"different-existing-archive")
        with self.assertRaises(ValueError):
            upload_storage.journal_directory()
        self.assertEqual(self.target.read_bytes(), b"different-existing-archive")
        self.assertEqual(json.loads(journal.read_text()), self.migrated_row())
        self.assertFalse((self.user / "sidecar_migration.json").exists())

    def test_journal_comparison_preserves_boolean_and_number_types(self):
        journal = self.destination / "session1.json"
        source_row = {**self.row, "finalized": True}
        (self.legacy / "session1.json").write_text(json.dumps(source_row))
        journal.write_text(json.dumps({**self.migrated_row(), "finalized": 1}))
        with self.assertRaises(ValueError):
            upload_storage.journal_directory()
        self.assertEqual(json.loads(journal.read_text())["finalized"], 1)
        self.assertFalse((self.user / "sidecar_migration.json").exists())

    def test_concurrent_journal_creation_is_not_overwritten_or_acknowledged(self):
        journal = self.destination / "session1.json"
        content = json.dumps({**self.migrated_row(), "account_email": "other@example.com"})
        original_link = upload_storage.os.link

        def create_before_publication(source, destination, *args, **kwargs):
            if Path(destination) == journal:
                journal.write_text(content)
            return original_link(source, destination, *args, **kwargs)

        with patch.object(upload_storage.os, "link", side_effect=create_before_publication), self.assertRaises(ValueError):
            upload_storage.journal_directory()
        self.assertEqual(journal.read_text(), content)
        self.assertEqual(self.source.read_bytes(), b"source-archive")
        self.assertFalse((self.user / "sidecar_migration.json").exists())


if __name__ == "__main__":
    unittest.main()
