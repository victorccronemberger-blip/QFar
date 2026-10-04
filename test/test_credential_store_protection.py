"""Credential protection/migration tests: fixture data and temporary roots only."""
import json
import os
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

from moneymin import credential_store, secure_store


class CredentialProtectionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="qmoney-credential-fixture-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.email = "fixture@example.invalid"
        self.password = "fixture-only-password-ç"

    def legacy(self, email=None, password=None):
        email = email or self.email
        path = credential_store.record_path(self.root, email)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": 1, "email": email,
                                    "password": password or self.password}), encoding="utf-8")
        return path

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI integration")
    def test_real_dpapi_record_contains_no_plaintext_and_round_trips(self):
        credential_store.save(self.root, self.email, self.password)
        path = credential_store.record_path(self.root, self.email)
        raw = path.read_bytes()
        self.assertNotIn(self.password.encode("utf-8"), raw)
        self.assertNotIn(self.email.encode("utf-8"), raw)
        with self.assertRaises((UnicodeError, ValueError)):
            json.loads(raw.decode("utf-8"))
        self.assertEqual(credential_store.lookup(self.root, self.email.upper(), strict=True), self.password)
        self.assertEqual(credential_store.load_all(self.root), {self.email: self.password})

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI integration")
    def test_legacy_lookup_migrates_in_place_without_plaintext_backup(self):
        path = self.legacy()
        original = path.read_bytes()
        self.assertEqual(credential_store.lookup(self.root, self.email, strict=True), self.password)
        encrypted = path.read_bytes()
        self.assertNotEqual(encrypted, original)
        self.assertNotIn(self.password.encode("utf-8"), encrypted)
        self.assertEqual(list(path.parent.iterdir()), [path])
        # Reading the protected record again is idempotent; no needless rewrite.
        self.assertEqual(credential_store.lookup(self.root, self.email), self.password)
        self.assertEqual(path.read_bytes(), encrypted)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI integration")
    def test_save_of_same_legacy_password_still_encrypts(self):
        path = self.legacy()
        credential_store.save(self.root, self.email, self.password)
        self.assertNotIn(self.password.encode("utf-8"), path.read_bytes())
        self.assertEqual(credential_store.lookup(self.root, self.email, strict=True), self.password)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI integration")
    def test_bulk_load_migrates_valid_record_but_preserves_corrupt_and_renamed(self):
        path = self.legacy()
        invalid = path.parent / ("0" * 64 + ".json")
        invalid.write_bytes(b"{invalid fixture")
        renamed = self.legacy("other@example.invalid", "other-fixture-only")
        renamed = renamed.rename(path.parent / ("1" * 64 + ".json"))
        before = renamed.read_bytes()
        self.assertEqual(credential_store.load_all(self.root), {self.email: self.password})
        self.assertNotIn(self.password.encode("utf-8"), path.read_bytes())
        self.assertEqual(invalid.read_bytes(), b"{invalid fixture")
        self.assertEqual(renamed.read_bytes(), before)

    def test_failed_protection_preserves_legacy_and_blocks_strict_fallback(self):
        path = self.legacy()
        before = path.read_bytes()
        with patch.object(secure_store, "_crypt", side_effect=RuntimeError(self.password)):
            self.assertIsNone(credential_store.lookup(self.root, self.email))
            with self.assertRaises(ValueError) as raised:
                credential_store.lookup(self.root, self.email, strict=True)
            self.assertNotIn(self.password, str(raised.exception))
            self.assertEqual(credential_store.load_all(self.root), {})
            with self.assertRaises(ValueError):
                credential_store.save(self.root, self.email, "replacement-fixture")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_protected_candidate_is_verified_before_legacy_is_replaced(self):
        path = self.legacy()
        before = path.read_bytes()
        # The provider returns an opaque blob but cannot round-trip its object.
        with patch.object(secure_store, "_crypt", side_effect=[b"opaque", b"{}"]), \
             patch.object(credential_store, "save_bytes") as write:
            with self.assertRaises(ValueError):
                credential_store.lookup(self.root, self.email, strict=True)
            write.assert_not_called()
        self.assertEqual(path.read_bytes(), before)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI integration")
    def test_failed_atomic_write_keeps_previous_record_and_hides_error_payload(self):
        path = self.legacy()
        before = path.read_bytes()
        with patch.object(credential_store, "save_bytes", side_effect=OSError(self.password)):
            with self.assertRaises(ValueError) as raised:
                credential_store.lookup(self.root, self.email, strict=True)
            self.assertNotIn(self.password, "".join(traceback.format_exception(raised.exception)))
        self.assertEqual(path.read_bytes(), before)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI integration")
    def test_save_is_rejected_if_written_record_cannot_be_read_back(self):
        path = self.legacy()
        before = path.read_bytes()
        # A no-op writer models an unconfirmed persistence result.
        with patch.object(credential_store, "save_bytes"):
            with self.assertRaises(OSError):
                credential_store.save(self.root, self.email, "new-fixture-password")
        self.assertEqual(path.read_bytes(), before)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI integration")
    def test_unreadable_dpapi_record_is_preserved_and_never_replaced(self):
        credential_store.save(self.root, self.email, self.password)
        path = credential_store.record_path(self.root, self.email)
        before = path.read_bytes()
        with patch.object(secure_store, "_crypt", side_effect=OSError("other-user-fixture")):
            self.assertIsNone(credential_store.lookup(self.root, self.email))
            with self.assertRaises(ValueError):
                credential_store.lookup(self.root, self.email, strict=True)
            with self.assertRaises(ValueError):
                credential_store.save(self.root, self.email, "replacement-fixture")
        self.assertEqual(path.read_bytes(), before)

    def test_plaintext_schema2_cannot_downgrade_protected_format(self):
        path = self.legacy()
        path.write_text(json.dumps({"schema": 2, "email": self.email,
                                    "password": self.password}), encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            credential_store.lookup(self.root, self.email, strict=True)
        with self.assertRaises(ValueError):
            credential_store.save(self.root, self.email, "replacement-fixture")
        self.assertEqual(path.read_bytes(), before)

    def test_read_access_failure_is_not_treated_as_missing_credential(self):
        path = self.legacy()
        before = path.read_bytes()
        with patch.object(Path, "read_bytes", side_effect=PermissionError(self.password)), \
             patch.object(Path, "exists", return_value=False), \
             patch.object(credential_store, "save_bytes") as write:
            with self.assertRaises(ValueError):
                credential_store.lookup(self.root, self.email, strict=True)
            with self.assertRaises(ValueError):
                credential_store.save(self.root, self.email, "replacement-fixture")
            write.assert_not_called()
        self.assertEqual(path.read_bytes(), before)

    def test_provider_errors_do_not_include_payload_in_public_traceback(self):
        with patch.object(secure_store, "_crypt", side_effect=OSError(self.password)):
            for operation in (
                lambda: secure_store.protect_json({"password": self.password}),
                lambda: secure_store.unprotect_json(b"opaque-fixture"),
            ):
                with self.assertRaises(secure_store.SecureStoreError) as raised:
                    operation()
                self.assertNotIn(self.password, "".join(traceback.format_exception(raised.exception)))

    def test_missing_dpapi_never_falls_back_to_plaintext(self):
        with patch.object(secure_store.os, "name", "posix"):
            with self.assertRaises(ValueError):
                secure_store.protect_json({"schema": 2, "password": self.password})
        self.assertEqual(list(self.root.iterdir()), [])
