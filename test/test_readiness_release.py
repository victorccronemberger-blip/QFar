from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from moneymin import readiness


class ReleaseReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [
            mock.patch.object(readiness.config, "ROOT", self.root),
            mock.patch.object(readiness.config, "MEDIA_DATA_DIR", self.root / "data"),
            mock.patch.object(readiness, "_binary_works", return_value=True),
            mock.patch.object(readiness, "_private_browser_present", return_value=True),
            mock.patch.object(readiness, "_valid_account_tokens", return_value=(1, 1)),
            mock.patch.object(
                readiness.shutil,
                "disk_usage",
                return_value=SimpleNamespace(free=40 * 1024**3),
            ),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def test_missing_ego4d_catalog_is_prepared_by_release_when_credentials_exist(self):
        with mock.patch.object(readiness, "_aws_credentials_present", return_value=True):
            result = readiness.campaign_readiness("ego4d")

        checks = {check["name"]: check for check in result["checks"]}
        catalog = checks["Catálogo Ego4D"]
        self.assertEqual(catalog["status"], "warning")
        self.assertIn("automaticamente", catalog["detail"])
        self.assertNotIn("scripts", catalog["detail"].casefold())
        self.assertTrue(result["ready"])

    def test_missing_catalog_and_credentials_stays_blocked_with_ui_instruction(self):
        with mock.patch.object(readiness, "_aws_credentials_present", return_value=False):
            result = readiness.campaign_readiness("ego4d")

        checks = {check["name"]: check for check in result["checks"]}
        catalog = checks["Catálogo Ego4D"]
        self.assertEqual(catalog["status"], "error")
        self.assertIn("Integrações", catalog["detail"])
        self.assertNotIn("scripts", catalog["detail"].casefold())
        self.assertFalse(result["ready"])

    def test_release_instructions_never_reference_developer_scripts(self):
        with mock.patch.object(readiness, "_aws_credentials_present", return_value=True), \
             mock.patch.object(readiness.holoassist, "annotations_path", return_value=self.root / "missing.json"), \
             mock.patch.object(readiness.holoassist, "data_dir", return_value=self.root / "holoassist"):
            result = readiness.campaign_readiness("all")

        details = " ".join(check["detail"] for check in result["checks"]).casefold()
        self.assertNotIn("scripts", details)
        self.assertNotIn(".bat", details)
        holo = next(check for check in result["checks"]
                    if check["name"] == "Catálogo HoloAssist")
        self.assertEqual(holo["status"], "warning")
        self.assertIn("automaticamente", holo["detail"])


if __name__ == "__main__":
    unittest.main()
