import unittest
import json
import tempfile
from pathlib import Path
from unittest import mock

from moneymin import device_catalog, device_profile


class DeviceCatalogTests(unittest.TestCase):
    def setUp(self):
        from moneymin import config
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.anchor_path = Path(directory.name) / "anchor.json"
        self.anchor = {"schemaVersion": 2, "device_model": "SM-S901E",
                       "os_version": "16", "sdk_int": 36,
                       "shell_android_id": "0011223344556677",
                       "minute_app_android_id": None}
        self.anchor_path.write_text(json.dumps(self.anchor), encoding="utf-8")
        patcher = mock.patch.object(config, "DEVICE_ANCHOR_PATH", self.anchor_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_catalog_covers_s21_to_s24_global_b(self):
        models = {m["buildModel"] for m in device_catalog.catalog_models()}
        for required in ("SM-G991B", "SM-S901B", "SM-S901E", "SM-S911B",
                         "SM-S921B", "SM-S918B", "SM-S928B"):
            self.assertIn(required, models)

    def test_api_levels_match_public_android_table(self):
        self.assertEqual(device_catalog.api_level_for_release("14"), 34)
        self.assertEqual(device_catalog.api_level_for_release("15"), 35)
        self.assertEqual(device_catalog.api_level_for_release("16"), 36)

    def test_identity_is_deterministic_per_email(self):
        a = device_catalog.generate_identity("alpha@example.invalid")
        b = device_catalog.generate_identity("alpha@example.invalid")
        c = device_catalog.generate_identity("beta@example.invalid")
        self.assertEqual(a, b)
        self.assertNotEqual(a["device_id"], c["device_id"])
        self.assertTrue(a["device_id"].startswith("android.ssaid:"))
        self.assertEqual(len(a["device_id"].split(":", 1)[1]), 16)
        self.assertEqual(
            a["sdk_int"], device_catalog.api_level_for_release(a["os_version"]))

    def test_device_pool_mirrors_catalog(self):
        pool_models = {row[1] for row in device_profile.DEVICE_POOL}
        catalog_models = {m["buildModel"] for m in device_catalog.catalog_models()}
        self.assertEqual(pool_models, catalog_models)

    def test_s22_can_draw_android_16(self):
        s22 = next(m for m in device_catalog.catalog_models()
                   if m["buildModel"] == "SM-S901B")
        self.assertIn("16", s22["osReleases"])

    def test_anchor_propagation_locks_family_and_varies_ids(self):
        from moneymin import config
        anchor = device_catalog.load_anchor()
        self.assertIsNotNone(anchor)
        with mock.patch.object(config, "DEVICE_PROPAGATE_FROM_ANCHOR", True), \
             mock.patch.object(config, "DEVICE_ANCHOR_OWNER_EMAIL", ""):
            a = device_catalog.generate_identity("a@example.invalid", from_anchor=True)
            b = device_catalog.generate_identity("b@example.invalid", from_anchor=True)
        self.assertTrue(a["from_anchor"])
        self.assertEqual(a["device_model"], "SM-S901E")
        self.assertEqual(a["os_version"], "16")
        self.assertEqual(a["sdk_int"], 36)
        self.assertEqual(a["device_id_source"], "shell_seeded")
        self.assertNotEqual(a["device_id"], b["device_id"])
        self.assertNotEqual(a["device_id"], "android.ssaid:0011223344556677")
        # Lastro no shell: diferente do hash só-e-mail.
        self.assertNotEqual(a["device_id"], device_catalog.device_id_for_email("a@example.invalid"))
        self.assertEqual(
            a["device_id"],
            device_catalog.device_id_from_shell_seed("0011223344556677", "a@example.invalid"))

    def test_anchor_owner_ignores_unverified_shell_android_id(self):
        from moneymin import config
        with mock.patch.object(config, "DEVICE_PROPAGATE_FROM_ANCHOR", True), \
             mock.patch.object(config, "DEVICE_ANCHOR_OWNER_EMAIL", "owner@example.invalid"):
            owned = device_catalog.generate_identity("owner@example.invalid", from_anchor=True)
        # Sem minute_app_android_id, usa seed do shell — nunca o valor cru.
        self.assertFalse(owned["anchor_owner"])
        self.assertEqual(owned["device_id_source"], "shell_seeded")
        self.assertNotEqual(owned["device_id"], "android.ssaid:0011223344556677")

    def test_anchor_id_is_reported_not_automatically_verified(self):
        from moneymin import config
        fake = {
            "device_model": "SM-S901E", "os_version": "16", "sdk_int": 36,
            "product_device": "r0q", "commercial": "Galaxy S22",
            "minute_app_android_id": "aabbccddeeff0011",
            "profileTemplate": {
                "device_model": "SM-S901E", "os_version": "16", "sdk_int": 36,
                "logical_camera_id": "3", "frames_gop": 30, "video_bitrate_mbps": 8.0,
            },
        }
        with mock.patch.object(config, "DEVICE_PROPAGATE_FROM_ANCHOR", True), \
             mock.patch.object(config, "DEVICE_ANCHOR_OWNER_EMAIL", "owner@example.invalid"), \
             mock.patch.object(device_catalog, "load_anchor", return_value=fake):
            owned = device_catalog.generate_identity("owner@example.invalid", from_anchor=True)
            sibling = device_catalog.generate_identity("other@example.invalid", from_anchor=True)
        self.assertTrue(owned["anchor_owner"])
        self.assertEqual(owned["device_id_source"], "anchor_reported")
        self.assertEqual(owned["device_id"], "android.ssaid:aabbccddeeff0011")
        self.assertFalse(sibling["anchor_owner"])
        self.assertNotEqual(sibling["device_id"], owned["device_id"])

    def test_invalid_anchor_schema_and_id_are_rejected(self):
        for change in ({"schemaVersion": 1}, {"schemaVersion": True},
                       {"minute_app_android_id": "not-an-id"},
                       {"minute_app_android_id": {"value": "0011223344556677"}}):
            with self.subTest(change=change):
                self.anchor_path.write_text(json.dumps({**self.anchor, **change}), encoding="utf-8")
                self.assertIsNone(device_catalog.load_anchor())

    def test_profile_roundtrip_preserves_provenance_and_old_ids(self):
        profile = device_profile.DeviceProfile(
            email="fixture@example.invalid", device_id="existing-id",
            device_id_source="shell_seeded", from_anchor=True)
        restored = device_profile.DeviceProfile.from_dict(profile.to_dict())
        self.assertEqual(restored.device_id_source, "shell_seeded")
        self.assertTrue(restored.from_anchor)
        legacy = device_profile.DeviceProfile.from_dict({"email": profile.email, "device_id": "old-id"})
        self.assertEqual(legacy.device_id, "old-id")
        self.assertEqual(legacy.device_id_source, "unknown")
