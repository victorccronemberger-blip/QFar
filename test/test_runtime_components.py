import unittest
from unittest.mock import patch

from moneymin import readiness


class RuntimeComponentsTests(unittest.TestCase):
    def test_installed_curl_library_loads_without_network(self):
        self.assertTrue(readiness._curl_present())

    def test_missing_command_does_not_start_a_process(self):
        with patch.object(readiness.subprocess, "run") as run:
            for command in (None, "", "   "):
                self.assertFalse(readiness._binary_works(command))
            run.assert_not_called()

    def test_reports_missing_components_without_account_or_network_access(self):
        with patch.object(readiness, "ffmpeg_bin", return_value="ffmpeg"), \
             patch.object(readiness, "ffprobe_bin", return_value=None), \
             patch.object(readiness, "_binary_works", side_effect=[True, False]), \
             patch.object(readiness, "_private_browser_present", return_value=False), \
             patch.object(readiness, "_curl_present", return_value=True), \
             patch.object(readiness, "_valid_account_tokens") as accounts:
            result = readiness.runtime_readiness()
        self.assertFalse(result["ready"])
        self.assertEqual(result["missing"], ["ffprobe", "browser"])
        accounts.assert_not_called()

    def test_all_components_available(self):
        with patch.object(readiness, "ffmpeg_bin", return_value="ffmpeg"), \
             patch.object(readiness, "ffprobe_bin", return_value="ffprobe"), \
             patch.object(readiness, "_binary_works", return_value=True), \
             patch.object(readiness, "_private_browser_present", return_value=True), \
             patch.object(readiness, "_curl_present", return_value=True):
            result = readiness.runtime_readiness()
        self.assertTrue(result["ready"])
        self.assertEqual(result["missing"], [])
