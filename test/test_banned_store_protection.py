"""Temporary archived-account fixtures; no real credentials or network calls."""
import copy
import json
import os
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

from moneymin import account_bans, banned_store, secure_store


class BannedStoreProtectionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="qmoney-banned-fixture-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.path = self.root / "banned_accounts.json"
        self.password = "archived-fixture-only-password-ç"
        self.document = {"schema": 1, "accounts": [{
            "email": "archived@example.invalid", "password": self.password,
            "restriction_confirmed": True, "monitor": {"status": "banned"},
        }]}

    def legacy(self):
        self.path.write_text(json.dumps(self.document, ensure_ascii=False), encoding="utf-8")
        return self.path.read_bytes()

    def test_missing_file_returns_independent_default_without_writing(self):
        default = {"schema": 1, "accounts": []}
        loaded = banned_store.load(self.path, default)
        self.assertEqual(loaded, default)
        loaded["accounts"].append({"email": "fixture@example.invalid"})
        self.assertEqual(default["accounts"], [])
        self.assertFalse(self.path.exists())

    def test_legacy_read_preserves_exact_bytes_and_restrictions(self):
        before = self.legacy()
        self.assertEqual(banned_store.load(self.path), self.document)
        with patch.object(account_bans.config, "DATA_DIR", self.root):
            self.assertEqual(account_bans.banned_emails(), {"archived@example.invalid"})
            with self.assertRaises(ValueError):
                account_bans.require_not_banned("ARCHIVED@example.invalid")
        self.assertEqual(self.path.read_bytes(), before)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI integration")
    def test_next_save_migrates_legacy_in_place_and_preserves_export_object(self):
        before = self.legacy()
        banned_store.save(self.path, self.document)
        raw = self.path.read_bytes()
        self.assertNotEqual(raw, before)
        self.assertNotIn(self.password.encode("utf-8"), raw)
        self.assertNotIn(b"archived@example.invalid", raw)
        self.assertEqual(banned_store.load(self.path), self.document)
        self.assertEqual(list(self.root.iterdir()), [self.path])
        with patch.object(account_bans.config, "DATA_DIR", self.root):
            self.assertEqual(account_bans.banned_emails(), {"archived@example.invalid"})

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI integration")
    def test_monitor_updates_do_not_reintroduce_plaintext(self):
        banned_store.save(self.path, self.document)
        loaded = banned_store.load(self.path)
        loaded["accounts"][0]["monitor"] = {"status": "unbanned", "balance_stale": False}
        banned_store.save(self.path, loaded)
        self.assertNotIn(self.password.encode("utf-8"), self.path.read_bytes())
        self.assertEqual(banned_store.load(self.path), loaded)

    def test_invalid_existing_file_never_becomes_empty_or_is_replaced(self):
        for raw in (b"{broken", b"[]", b"null", b'{"accounts":{}}',
                    b'{"accounts":[{"email":7}]}', b"\xff"):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                with self.assertRaises(banned_store.BannedStoreError):
                    banned_store.load(self.path, {"accounts": []})
                with self.assertRaises(banned_store.BannedStoreError):
                    banned_store.save(self.path, self.document)
                self.assertEqual(self.path.read_bytes(), raw)
                with patch.object(account_bans.config, "DATA_DIR", self.root), \
                     self.assertRaises(banned_store.BannedStoreError):
                    account_bans.require_not_banned("other@example.invalid")

    def test_invalid_new_schema_cannot_replace_readable_legacy(self):
        before = self.legacy()
        for invalid in ({}, {"accounts": None}, {"accounts": [None]},
                        {"accounts": [{"email": ""}]}):
            with self.subTest(document=invalid), self.assertRaises(ValueError):
                banned_store.save(self.path, invalid)
        self.assertEqual(self.path.read_bytes(), before)

    def test_protection_failure_preserves_legacy_without_exposing_password(self):
        before = self.legacy()
        with patch.object(secure_store, "_crypt", side_effect=RuntimeError(self.password)):
            with self.assertRaises(banned_store.BannedStoreError) as raised:
                banned_store.save(self.path, self.document)
        self.assertNotIn(self.password, "".join(traceback.format_exception(raised.exception)))
        self.assertEqual(self.path.read_bytes(), before)

    def test_candidate_roundtrip_failure_happens_before_replace(self):
        before = self.legacy()
        with patch.object(secure_store, "_crypt", side_effect=[b"opaque", b"{}"]), \
             patch.object(banned_store, "save_bytes") as write:
            with self.assertRaises(banned_store.BannedStoreError):
                banned_store.save(self.path, self.document)
            write.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_access_failure_is_not_an_empty_banlist(self):
        before = self.legacy()
        with patch.object(Path, "read_bytes", side_effect=PermissionError(self.password)):
            with self.assertRaises(banned_store.BannedStoreError) as raised:
                banned_store.load(self.path, {"accounts": []})
            with self.assertRaises(banned_store.BannedStoreError):
                banned_store.save(self.path, self.document)
        self.assertNotIn(self.password, str(raised.exception))
        self.assertEqual(self.path.read_bytes(), before)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI integration")
    def test_other_user_or_corrupt_dpapi_cannot_remove_ban_restrictions(self):
        banned_store.save(self.path, self.document)
        before = self.path.read_bytes()
        with patch.object(secure_store, "_crypt", side_effect=OSError(self.password)):
            with self.assertRaises(banned_store.BannedStoreError) as raised:
                banned_store.load(self.path, {"accounts": []})
            with self.assertRaises(banned_store.BannedStoreError):
                banned_store.save(self.path, {"schema": 1, "accounts": []})
            with patch.object(account_bans.config, "DATA_DIR", self.root), \
                 self.assertRaises(banned_store.BannedStoreError):
                account_bans.require_not_banned("other@example.invalid")
        self.assertNotIn(self.password, "".join(traceback.format_exception(raised.exception)))
        self.assertEqual(self.path.read_bytes(), before)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI integration")
    def test_failed_atomic_write_keeps_archived_recovery_data(self):
        banned_store.save(self.path, self.document)
        before = self.path.read_bytes()
        updated = copy.deepcopy(self.document)
        updated["accounts"][0]["monitor"]["status"] = "unbanned"
        with patch.object(banned_store, "save_bytes", side_effect=OSError(self.password)):
            with self.assertRaises(banned_store.BannedStoreError):
                banned_store.save(self.path, updated)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(banned_store.load(self.path), self.document)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI integration")
    def test_unconfirmed_write_cannot_report_success(self):
        before = self.legacy()
        with patch.object(banned_store, "save_bytes"):
            with self.assertRaises(banned_store.BannedStoreError):
                banned_store.save(self.path, self.document)
        self.assertEqual(self.path.read_bytes(), before)
