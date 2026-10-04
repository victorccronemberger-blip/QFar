"""Archived-password type contracts with artificial local bytes and no DPAPI."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moneymin import banned_store, secure_store


INVALID_PASSWORDS = (True, False, 0, 23, 1.5, [], ['fixture'], {}, {'fixture': 1})
ABSENT = object()
VALID_PASSWORDS = (ABSENT, None, '', ' fixture-Password:áß<>! ')


def document(password=ABSENT):
    row = {'email': 'archived@example.invalid', 'restriction_confirmed': True,
           'removed_at': 'fixture-original-removal', 'monitor': {'status': 'banned'},
           'fixture_unknown': {'preserve': [1, None, 'á']}}
    if password is not ABSENT:
        row['password'] = password
    return {'schema': 1, 'accounts': [row], 'fixture_unknown': ['retain']}


class BannedPasswordSchemaTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='banned-schema-artificial-')
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.path = self.root / 'banned_accounts.json'
        self.tombstone = self.root / 'removed_accounts.json'
        self.tombstone.write_bytes(b'{"removed":["archived@example.invalid"]}\r\n')
        self.tombstone_before = self.tombstone.read_bytes()
        self.valid = document('fixture-valid-password')

    def write_legacy(self, value):
        raw = ('\ufeff' + json.dumps(value, ensure_ascii=False, indent=2) + '\r\n').encode('utf-8')
        self.path.write_bytes(raw)
        return raw

    def assert_tombstone_preserved(self):
        self.assertEqual(self.tombstone.read_bytes(), self.tombstone_before)

    def test_validator_rejects_nontextual_known_password(self):
        for password in INVALID_PASSWORDS:
            with self.subTest(password_type=type(password).__name__, value=repr(password)):
                value = document(password)
                before = copy.deepcopy(value)
                with self.assertRaises(banned_store.BannedStoreError):
                    banned_store._validate(value)
                self.assertEqual(value, before)

    def test_legacy_invalid_password_preserves_bytes_without_dpapi_or_write(self):
        for password in INVALID_PASSWORDS:
            with self.subTest(password_type=type(password).__name__, value=repr(password)):
                before = self.write_legacy(document(password))
                with patch.object(secure_store, '_crypt', side_effect=AssertionError('No actual DPAPI.')) as crypt, \
                     patch.object(banned_store, 'save_bytes', side_effect=AssertionError('No publication.')) as write:
                    with self.assertRaises(banned_store.BannedStoreError):
                        banned_store.load(self.path, {'accounts': []})
                    crypt.assert_not_called()
                    write.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)
                self.assert_tombstone_preserved()

    def test_protected_decoded_invalid_password_is_rejected_without_write(self):
        for password in INVALID_PASSWORDS:
            with self.subTest(password_type=type(password).__name__, value=repr(password)):
                before = banned_store._MAGIC + b'artificial-opaque-archive'
                self.path.write_bytes(before)
                with patch.object(secure_store, 'unprotect_json', return_value=document(password)) as decode, \
                     patch.object(secure_store, '_crypt', side_effect=AssertionError('No actual DPAPI.')) as crypt, \
                     patch.object(banned_store, 'save_bytes', side_effect=AssertionError('No publication.')) as write:
                    with self.assertRaises(banned_store.BannedStoreError):
                        banned_store.load(self.path)
                    decode.assert_called_once_with(b'artificial-opaque-archive')
                    crypt.assert_not_called()
                    write.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)
                self.assert_tombstone_preserved()

    def test_invalid_candidate_rejects_before_protection_and_replace(self):
        for password in INVALID_PASSWORDS:
            with self.subTest(password_type=type(password).__name__, value=repr(password)):
                before = self.write_legacy(self.valid)
                with patch.object(secure_store, 'protect_json', side_effect=secure_store.SecureStoreError('Candidate protection tripwire.')) as protect, \
                     patch.object(banned_store, 'save_bytes', side_effect=AssertionError('Candidate must reject before publication.')) as write:
                    with self.assertRaises(banned_store.BannedStoreError):
                        banned_store.save(self.path, document(password))
                    protect.assert_not_called()
                    write.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)
                self.assert_tombstone_preserved()

    def test_invalid_candidate_cannot_create_an_absent_archive(self):
        for password in INVALID_PASSWORDS:
            with self.subTest(password_type=type(password).__name__, value=repr(password)):
                with patch.object(secure_store, 'protect_json', side_effect=secure_store.SecureStoreError('Absent candidate protection tripwire.')) as protect, \
                     patch.object(banned_store, 'save_bytes', side_effect=AssertionError('No publication for bad schema.')) as write:
                    with self.assertRaises(banned_store.BannedStoreError):
                        banned_store.save(self.path, document(password))
                    protect.assert_not_called()
                    write.assert_not_called()
                self.assertFalse(self.path.exists())
                self.assert_tombstone_preserved()

    def test_invalid_existing_password_blocks_valid_replacement(self):
        for password in INVALID_PASSWORDS:
            with self.subTest(password_type=type(password).__name__, value=repr(password)):
                before = self.write_legacy(document(password))
                with patch.object(secure_store, 'protect_json', side_effect=secure_store.SecureStoreError('Existing schema protection tripwire.')) as protect, \
                     patch.object(banned_store, 'save_bytes', side_effect=AssertionError('Prior bad schema must reject before replacement.')) as write:
                    with self.assertRaises(banned_store.BannedStoreError):
                        banned_store.save(self.path, self.valid)
                    protect.assert_not_called()
                    write.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)
                self.assert_tombstone_preserved()

    def test_legacy_optional_text_passwords_retain_exact_object_and_bytes(self):
        for password in VALID_PASSWORDS:
            with self.subTest(password_type='absent' if password is ABSENT else type(password).__name__, value=repr(password)):
                value = document(password)
                before = self.write_legacy(value)
                with patch.object(secure_store, '_crypt', side_effect=AssertionError('Legacy read must not call DPAPI.')) as crypt, \
                     patch.object(banned_store, 'save_bytes', side_effect=AssertionError('Read must not publish.')) as write:
                    self.assertEqual(banned_store.load(self.path), value)
                    crypt.assert_not_called()
                    write.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)
                self.assert_tombstone_preserved()

    def test_optional_text_passwords_roundtrip_through_real_json_helpers(self):
        def artificial_crypt(payload, *, protect):
            # Reversible fixture codec, deliberately unrelated to Windows DPAPI.
            return b'artificial:' + payload if protect else payload.removeprefix(b'artificial:')
        for password in VALID_PASSWORDS:
            with self.subTest(password_type='absent' if password is ABSENT else type(password).__name__, value=repr(password)):
                value = document(password)
                self.write_legacy(value)
                with patch.object(secure_store, '_crypt', side_effect=artificial_crypt) as crypt:
                    banned_store.save(self.path, value)
                    self.assertEqual(banned_store.load(self.path), value)
                    self.assertGreaterEqual(crypt.call_count, 3)
                self.assertTrue(self.path.read_bytes().startswith(banned_store._MAGIC))
                self.assert_tombstone_preserved()

    def test_missing_archive_default_is_independent_and_unwritten(self):
        default = {'schema': 1, 'accounts': []}
        value = banned_store.load(self.path, default)
        value['accounts'].append({'email': 'artificial@example.invalid'})
        self.assertEqual(default, {'schema': 1, 'accounts': []})
        self.assertFalse(self.path.exists())
        self.assert_tombstone_preserved()

    def test_existing_email_schema_controls_remain_rejected(self):
        for bad_row in ({}, {'email': None}, {'email': 3}, {'email': ''}, {'email': '  '}):
            with self.subTest(row=bad_row):
                before = self.write_legacy({'accounts': [bad_row]})
                with self.assertRaises(banned_store.BannedStoreError):
                    banned_store.load(self.path)
                self.assertEqual(self.path.read_bytes(), before)
                self.assert_tombstone_preserved()


if __name__ == '__main__':
    unittest.main()
