import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moneymin import config, upload, upload_storage


class UploadStorageIsolationTests(unittest.TestCase):
    def test_invalid_session_identifiers_cannot_escape_journal_directory(self):
        for sid in ("../other", "a/b", "C:/private", "..", ""):
            with self.subTest(sid=sid), self.assertRaises(upload.UploadError):
                upload._sidecar_path(sid)

    def test_shared_library_imports_only_owned_sessions_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            library, user, tokens = root / "library", root / "user", root / "tokens"
            legacy = library / "sidecars"
            legacy.mkdir(parents=True)
            tokens.mkdir()
            (tokens / "token_a.json").write_text(json.dumps({"email": "a@example.com"}))
            (legacy / "own.json").write_text(json.dumps({"session_id": "own", "account_email": "a@example.com", "state": "pending"}))
            (legacy / "foreign.json").write_text(json.dumps({"session_id": "foreign", "account_email": "b@example.com"}))
            (legacy / "own.data.zip").write_bytes(b"archive-fixture")
            with patch.object(config, "DATA_DIR", user), patch.object(config, "MEDIA_DATA_DIR", library), \
                 patch.object(config, "tokens_dir", return_value=tokens):
                rows = upload.list_sidecars()
                self.assertEqual([row["session_id"] for row in rows], ["own"])
                archive = Path(rows[0]["sidecar_data_path"])
                self.assertEqual(archive.parent, user / "sidecars")
                self.assertEqual(archive.read_bytes(), b"archive-fixture")
                upload.save_sidecar({**rows[0], "state": "done"})
                self.assertEqual(upload.list_sidecars()[0]["state"], "done")
                (user / "sidecars" / "own.json").unlink()
                self.assertEqual(upload.list_sidecars(), [])
                self.assertTrue((legacy / "own.json").exists())
            with patch.object(config, "DATA_DIR", root / "other-user"), patch.object(config, "MEDIA_DATA_DIR", library), \
                 patch.object(config, "tokens_dir", return_value=root / "empty-tokens"):
                self.assertEqual(upload.list_sidecars(), [])

    def test_same_root_legacy_installation_needs_no_migration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(config, "DATA_DIR", root), patch.object(config, "MEDIA_DATA_DIR", root):
                upload.save_sidecar({"session_id": "legacy", "state": "done"})
                self.assertEqual(upload.list_sidecars()[0]["session_id"], "legacy")
                self.assertFalse((root / "sidecar_migration.json").exists())
