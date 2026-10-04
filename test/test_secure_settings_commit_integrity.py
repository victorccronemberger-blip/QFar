"""Integration-vault commit tests use synthetic data and temporary files only."""
import json
import math
import os
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

from moneymin import atomic_io, secure_store


class SecureSettingsCommitIntegrityTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="qmoney-vault-commit-fixture-")
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "integrations.dat"
        self.canary = "artificial-private-value-ç"
        self.original = {"schema": 2, "hostinger": {"token": self.canary}}
        self.path.write_bytes(self.encode(self.original))
        self.before = self.path.read_bytes()

    @staticmethod
    def encode(value):
        return b"fixture-encrypted:" + json.dumps(value, ensure_ascii=False).encode("utf-8")

    @staticmethod
    def crypt(value, *, protect):
        if protect:
            return b"fixture-encrypted:" + value
        return value.removeprefix(b"fixture-encrypted:")

    def assert_private(self, error):
        self.assertIsInstance(error, secure_store.SecureStoreError)
        self.assertNotIn(self.canary, str(error))
        self.assertNotIn(self.canary, "".join(traceback.format_exception(error)))
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__suppress_context__)

    def test_non_object_candidates_never_replace_valid_vault(self):
        for value in ([], [self.canary], None, self.canary, 1, True):
            with self.subTest(type=type(value).__name__), \
                    patch.object(secure_store, "_crypt", side_effect=self.crypt), \
                    patch.object(secure_store, "save_bytes") as write:
                with self.assertRaises(secure_store.SecureStoreError) as raised:
                    secure_store.save_secure_settings(self.path, value)
                self.assert_private(raised.exception)
                write.assert_not_called()
                self.assertEqual(self.path.read_bytes(), self.before)

    def test_invalid_json_candidates_never_commit_or_expose_values(self):
        circular = {"private": self.canary}
        circular["cycle"] = circular
        for value in ({"private": {self.canary}}, circular,
                      {"number": math.inf}, {"number": -math.inf},
                      {"number": math.nan}, {"tuple": (self.canary,)},
                      {1: self.canary}):
            with self.subTest(case=tuple(value)), \
                    patch.object(secure_store, "_crypt", side_effect=self.crypt), \
                    patch.object(secure_store, "save_bytes") as write:
                with self.assertRaises(secure_store.SecureStoreError) as raised:
                    secure_store.save_secure_settings(self.path, value)
                self.assert_private(raised.exception)
                write.assert_not_called()
                self.assertEqual(self.path.read_bytes(), self.before)

    def test_encryption_failure_preserves_vault_with_private_diagnostic(self):
        def failing_crypt(value, *, protect):
            if protect:
                raise RuntimeError(self.canary)
            return self.crypt(value, protect=False)
        with patch.object(secure_store, "_crypt", side_effect=failing_crypt), \
                patch.object(secure_store, "save_bytes") as write:
            with self.assertRaises(secure_store.SecureStoreError) as raised:
                secure_store.save_secure_settings(self.path, {"schema": 2})
            self.assert_private(raised.exception)
            write.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_mismatching_roundtrip_is_rejected_before_write(self):
        for encrypted, decrypted in ((b"opaque-fixture", b"{}"),
                                      (b"opaque-fixture", b"[]"),
                                      (b"opaque-fixture", b"\xff"),
                                      (b"", b"{}")):
            with self.subTest(decrypted=decrypted), \
                    patch.object(secure_store, "_crypt",
                                 side_effect=[self.before.removeprefix(b"fixture-encrypted:"),
                                              encrypted, decrypted]), \
                    patch.object(secure_store, "save_bytes") as write:
                with self.assertRaises(secure_store.SecureStoreError) as raised:
                    secure_store.save_secure_settings(self.path, {"schema": 2})
                self.assert_private(raised.exception)
                write.assert_not_called()
                self.assertEqual(self.path.read_bytes(), self.before)

    def test_missing_vault_and_invalid_candidate_do_not_create_parent(self):
        missing = self.path.parent / "missing" / "integrations.dat"
        with patch.object(secure_store, "_crypt") as crypt, \
                patch.object(secure_store, "save_bytes") as write:
            with self.assertRaises(secure_store.SecureStoreError) as raised:
                secure_store.save_secure_settings(missing, [self.canary])
            self.assert_private(raised.exception)
            crypt.assert_not_called()
            write.assert_not_called()
        self.assertFalse(missing.parent.exists())

    def test_existing_unreadable_vault_is_not_replaced_or_exposed(self):
        for exception in (OSError(self.canary), RuntimeError(self.canary),
                          ValueError(self.canary), TypeError(self.canary)):
            with self.subTest(exception=type(exception).__name__), \
                    patch.object(secure_store, "_crypt", side_effect=exception), \
                    patch.object(secure_store, "save_bytes") as write:
                with self.assertRaises(secure_store.SecureStoreError) as raised:
                    secure_store.save_secure_settings(self.path, {"schema": 2})
                self.assert_private(raised.exception)
                write.assert_not_called()
                self.assertEqual(self.path.read_bytes(), self.before)

    def test_atomic_writer_failure_is_private_and_preserves_vault(self):
        with patch.object(secure_store, "_crypt", side_effect=self.crypt), \
                patch.object(secure_store, "save_bytes", side_effect=OSError(self.canary)):
            with self.assertRaises(secure_store.SecureStoreError) as raised:
                secure_store.save_secure_settings(self.path, {"schema": 2})
            self.assert_private(raised.exception)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_fsync_failure_keeps_prior_vault_and_removes_temporary_file(self):
        with patch.object(secure_store, "_crypt", side_effect=self.crypt), \
                patch.object(atomic_io.os, "fsync", side_effect=OSError(self.canary)):
            with self.assertRaises(secure_store.SecureStoreError) as raised:
                secure_store.save_secure_settings(self.path, {"schema": 2})
            self.assert_private(raised.exception)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_replace_failure_keeps_prior_vault_and_removes_temporary_file(self):
        with patch.object(secure_store, "_crypt", side_effect=self.crypt), \
                patch.object(Path, "replace", side_effect=PermissionError(self.canary)):
            with self.assertRaises(secure_store.SecureStoreError) as raised:
                secure_store.save_secure_settings(self.path, {"schema": 2})
            self.assert_private(raised.exception)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_existing_invalid_json_or_non_object_is_preserved(self):
        for invalid in (b"{", b"[]", b"null", b"\xff"):
            with self.subTest(value=invalid), \
                    patch.object(secure_store, "_crypt", return_value=invalid), \
                    patch.object(secure_store, "save_bytes") as write:
                with self.assertRaises(secure_store.SecureStoreError) as raised:
                    secure_store.save_secure_settings(self.path, {"schema": 2})
                self.assert_private(raised.exception)
                write.assert_not_called()
                self.assertEqual(self.path.read_bytes(), self.before)

    def test_valid_object_and_section_update_preserve_other_sections(self):
        value = {"schema": 2, "hostinger": {"token": self.canary},
                 "ego4d": {"region": "fixture-only", "nested": [True, None, 1, 1.5]}}
        with patch.object(secure_store, "_crypt", side_effect=self.crypt):
            secure_store.save_secure_settings(self.path, value)
            self.assertEqual(secure_store.load_secure_settings(self.path, strict=True), value)
            updated = secure_store.update_secure_section(self.path, "ego4d", {"region": "changed"})
            self.assertEqual(updated["hostinger"], value["hostinger"])
            self.assertEqual(secure_store.load_secure_settings(self.path, strict=True), updated)
            removed = secure_store.update_secure_section(self.path, "ego4d", None)
            self.assertNotIn("ego4d", removed)
            self.assertEqual(removed["hostinger"], value["hostinger"])

    def test_nonstrict_loader_compatibility_is_read_only(self):
        with patch.object(secure_store, "_crypt", side_effect=OSError(self.canary)):
            self.assertEqual(secure_store.load_secure_settings(self.path), {})
            with self.assertRaises(secure_store.SecureStoreError) as raised:
                secure_store.load_secure_settings(self.path, strict=True)
            self.assert_private(raised.exception)
        self.assertEqual(self.path.read_bytes(), self.before)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI integration")
    def test_real_dpapi_commit_and_invalid_candidate_preserve_protected_bytes(self):
        path = self.path.parent / "perfil de teste ç" / "integrations.dat"
        secure_store.save_secure_settings(path, self.original)
        encrypted = path.read_bytes()
        self.assertNotIn(self.canary.encode("utf-8"), encrypted)
        self.assertEqual(secure_store.load_secure_settings(path, strict=True), self.original)
        with self.assertRaises(secure_store.SecureStoreError):
            secure_store.save_secure_settings(path, [self.canary])
        self.assertEqual(path.read_bytes(), encrypted)
        self.assertEqual(secure_store.load_secure_settings(path, strict=True), self.original)
