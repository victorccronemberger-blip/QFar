"""A close failure releases other handles and never replaces private diagnostics."""
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

from moneymin import capture_import as capture


class CaptureCleanupIntegrityTests(unittest.TestCase):
    def test_all_opened_streams_are_closed_and_primary_error_is_preserved(self):
        first, second = Mock(), Mock()
        first.close.side_effect = OSError("private-canary")
        stamp = Mock(st_dev=1, st_ino=1, st_size=5)
        other = Mock(st_dev=1, st_ino=2, st_size=5)
        with patch.object(capture, "_open_source", side_effect=[(Path("media"), first, stamp),
                                                               (Path("sidecar"), second, other)]), \
             patch.object(capture, "_file_hash", side_effect=capture.CaptureImportError("source_changed")):
            with self.assertRaises(capture.CaptureImportError) as raised:
                capture.inspect_original_capture("media", "sidecar")
        self.assertEqual(raised.exception.code, "source_changed")
        self.assertNotIn("private-canary", str(raised.exception))
        first.close.assert_called_once()
        second.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
