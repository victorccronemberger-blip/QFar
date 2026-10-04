"""Original journal identities must be checked before resolving writable storage."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moneymin import upload


class JournalCommitIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.row = {"session_id": "session1", "chunk_index": 0, "state": "done"}
        self.path = self.root / "session1.json"
        self.path.write_text(json.dumps(self.row), encoding="utf-8")

    def test_original_index_type_is_rejected_without_storage_resolution(self):
        for index in (False, True, None, 0.0, 0.5, "", "0", "1", [], {}, -1):
            with self.subTest(index=index):
                before = self.path.read_bytes()
                with patch.object(upload, "sidecars_dir") as directory, \
                     patch.object(upload, "save_json") as save:
                    with self.assertRaises(upload.UploadError):
                        upload.save_sidecar({**self.row, "chunk_index": index})
                directory.assert_not_called()
                save.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_root_or_owner_alias_is_rejected_before_io(self):
        values = (None, [], "private-canary", {},
                  {"session_id": "../private-canary", "chunk_index": 0},
                  {"session_id": [], "sessionId": "session1"},
                  {"session_id": "", "sessionId": "session1"},
                  {"session_id": "session1", "sessionId": "another"})
        for value in values:
            with self.subTest(value=value), patch.object(upload, "sidecars_dir") as directory, \
                 patch.object(upload, "save_json") as save:
                with self.assertRaises(upload.UploadError) as raised:
                    upload.save_sidecar(value)
                self.assertNotIn("private-canary", str(raised.exception))
                directory.assert_not_called()
                save.assert_not_called()

    def test_invalid_path_identity_never_resolves_sidecar_directory(self):
        for sid, index in (("../private-canary", 0), ("session1", False), ("session1", "0")):
            with self.subTest(sid=sid, index=index), patch.object(upload, "sidecars_dir") as directory:
                with self.assertRaises(upload.UploadError):
                    upload._sidecar_path(sid, index)
                directory.assert_not_called()

    def test_valid_indices_and_legacy_missing_zero_round_trip(self):
        with patch.object(upload, "sidecars_dir", return_value=self.root):
            for index in (0, 1, 999):
                row = {**self.row, "chunk_index": index}
                path = upload.save_sidecar(row)
                self.assertEqual(path.name, "session1.json" if index == 0 else f"session1__{index}.json")
                self.assertEqual(upload.load_sidecar("session1", index), row)
            legacy = {"session_id": "legacy", "state": "done"}
            self.assertEqual(upload.save_sidecar(legacy).name, "legacy.json")
            self.assertIn(legacy, upload.list_sidecars())

    def test_supported_camel_alias_is_saved_as_a_listable_canonical_copy(self):
        original = {"sessionId": "alias", "chunk_index": 1, "state": "done"}
        with patch.object(upload, "sidecars_dir", return_value=self.root):
            path = upload.save_sidecar(original)
            self.assertEqual(path.name, "alias__1.json")
            stored = {**original, "session_id": "alias"}
            self.assertEqual(upload.load_sidecar("alias", 1), stored)
            self.assertIn(stored, upload.list_sidecars())
        self.assertNotIn("session_id", original)

    def test_failed_atomic_commit_preserves_existing_bytes(self):
        before = self.path.read_bytes()
        with patch.object(upload, "sidecars_dir", return_value=self.root), \
             patch("moneymin.atomic_io.Path.replace", side_effect=PermissionError("fixture")):
            with self.assertRaises(PermissionError):
                upload.save_sidecar({**self.row, "state": "retry_late"})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()),
                         ["session1.json", "session1.write.lock"])
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_legacy_filename_collision_cannot_load_or_replace_another_session(self):
        first = {"session_id": "session__1", "chunk_index": 0, "state": "done"}
        second = {"session_id": "session", "chunk_index": 1, "state": "creating"}
        with patch.object(upload, "sidecars_dir", return_value=self.root):
            path = upload.save_sidecar(first)
            before = path.read_bytes()
            with self.assertRaises(upload.UploadError):
                upload.load_sidecar("session", 1)
            with self.assertRaises(upload.UploadError):
                upload.save_sidecar(second)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(upload.load_sidecar("session__1", 0), first)

    def test_existing_owner_context_cannot_be_changed_or_removed(self):
        first = {**self.row, "account_email": "one@example.invalid", "org_key": "org", "task_id": "task"}
        with patch.object(upload, "sidecars_dir", return_value=self.root):
            path = upload.save_sidecar(first)
            before = path.read_bytes()
            for key in ("account_email", "org_key", "task_id"):
                for remove in (False, True):
                    candidate = dict(first)
                    if remove:
                        candidate.pop(key)
                    else:
                        candidate[key] = "other"
                    with self.subTest(key=key, remove=remove), self.assertRaises(upload.UploadError):
                        upload.save_sidecar(candidate)
                    self.assertEqual(path.read_bytes(), before)

    def test_invalid_json_candidate_preserves_prior_journal(self):
        before = self.path.read_bytes()
        for value in (float("nan"), float("inf"), set()):
            with self.subTest(value=type(value).__name__), patch.object(upload, "sidecars_dir") as directory:
                with self.assertRaises(upload.UploadError):
                    upload.save_sidecar({**self.row, "extra": value})
                directory.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
