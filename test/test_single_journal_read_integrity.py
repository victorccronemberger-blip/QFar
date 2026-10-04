"""An existing unreadable journal is never absence of a previous send."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moneymin import upload


class SingleJournalReadIntegrityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        mocked = patch.object(upload, "sidecars_dir", return_value=self.root)
        mocked.start()
        self.addCleanup(mocked.stop)
        self.path = self.root / "session1.json"

    def test_missing_returns_none_and_valid_legacy_zero_is_read_only(self):
        self.assertIsNone(upload.load_sidecar("session1"))
        row = {"session_id": "session1", "state": "done"}
        self.path.write_text(json.dumps(row), encoding="utf-8-sig")
        before = self.path.read_bytes()
        self.assertEqual(upload.load_sidecar("session1"), row)
        self.assertEqual(self.path.read_bytes(), before)

    def test_corrupt_existing_journal_is_private_and_preserved(self):
        for raw in (b"\xff-private-canary", b'{"private-canary":', b"[]", b"null",
                    b'{"session_id":"other","chunk_index":0}',
                    b'{"session_id":"session1","chunk_index":false}',
                    b'{"session_id":"session1","chunk_index":1}', b'{}',
                    b'{"session_id":"other","session_id":"session1","chunk_index":0}',
                    b'{"session_id":"session1","chunk_index":0,"extra":NaN}'):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                with self.assertRaises(upload.UploadError) as raised:
                    upload.load_sidecar("session1")
                self.assertEqual(raised.exception.phase, "recovery")
                self.assertFalse(raised.exception.retryable)
                self.assertNotIn("private-canary", str(raised.exception))
                self.assertNotIn(str(self.root), str(raised.exception))
                self.assertEqual(self.path.read_bytes(), raw)

    def test_read_error_is_not_absence(self):
        self.path.write_text('{"session_id":"session1"}', encoding="utf-8")
        before = self.path.read_bytes()
        with patch.object(Path, "read_text", side_effect=PermissionError("private-canary")):
            with self.assertRaises(upload.UploadError) as raised:
                upload.load_sidecar("session1")
            self.assertNotIn("private-canary", str(raised.exception))
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
