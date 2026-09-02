from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from moneymin import crowtado


class PrivateBrowserTests(unittest.TestCase):
    def test_packaged_chromium_is_found_without_system_chrome(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chrome = root / "chromium-1234" / "chrome-win64" / "chrome.exe"
            chrome.parent.mkdir(parents=True)
            chrome.touch()

            with mock.patch.dict("os.environ", {
                "PLAYWRIGHT_BROWSERS_PATH": str(root),
            }, clear=False):
                self.assertEqual(Path(crowtado._playwright_chrome()), chrome)

    def test_packaged_browser_path_accepts_legacy_chrome_win_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chrome = root / "chromium-999" / "chrome-win" / "chrome.exe"
            chrome.parent.mkdir(parents=True)
            chrome.touch()

            with mock.patch.dict("os.environ", {
                "PLAYWRIGHT_BROWSERS_PATH": str(root),
            }, clear=False):
                self.assertEqual(Path(crowtado._playwright_chrome()), chrome)


if __name__ == "__main__":
    unittest.main()
