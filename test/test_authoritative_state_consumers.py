"""Corrupt account/history state blocks replacement and provider work."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from moneymin import account_transfer, config, org_migrate, sent_registry
from moneymin.atomic_io import JsonStateError
from moneymin.web.org_migration import OrgMigrationRunner


class AuthoritativeStateConsumerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patcher = patch.object(config, "DATA_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_account_mapping_does_not_hide_duplicate_or_unreadable_state(self):
        path = self.root / "accounts.json"
        for raw in (b'{"private-canary":', b'{"a":1,"a":2}', b'[]', b'{"x":NaN}'):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                with self.assertRaises(JsonStateError):
                    account_transfer._mapping(path)
                self.assertEqual(path.read_bytes(), raw)
        path.unlink()
        self.assertEqual(account_transfer._mapping(path), {})

    def test_org_preferences_are_preserved_before_commit(self):
        path = self.root / "prefs.json"
        for raw in (b'{"private-canary":', b'[]', b'{"org_keys":[]}', b'{"org_keys":null}'):
            with self.subTest(raw=raw), patch.object(org_migrate, "PREFS_PATH", path), \
                 patch.object(org_migrate, "save_json") as save:
                path.write_bytes(raw)
                with self.assertRaises(JsonStateError):
                    org_migrate._set_pref_org("one@example.invalid", "org")
                save.assert_not_called()
                self.assertEqual(path.read_bytes(), raw)
        path.unlink()
        with patch.object(org_migrate, "PREFS_PATH", path):
            org_migrate._set_pref_org("one@example.invalid", "org")
        self.assertEqual(json.loads(path.read_bytes())["org_keys"], {"one@example.invalid": "org"})

    def test_registry_missing_does_not_seed_from_corrupt_receipts(self):
        history = self.root / "campaign_fixture.json"
        raw = b'{"private-canary":'
        history.write_bytes(raw)
        with patch.object(sent_registry, "_save") as save:
            with self.assertRaises(JsonStateError):
                sent_registry.load()
        save.assert_not_called()
        self.assertEqual(history.read_bytes(), raw)
        self.assertFalse((self.root / sent_registry.FILE_NAME).exists())

    def test_corrupt_org_preferences_block_before_authentication_and_join(self):
        path = self.root / "prefs.json"
        path.write_bytes(b'{"org_keys":[]}')
        with patch.object(org_migrate, "PREFS_PATH", path), \
             patch.object(org_migrate, "session_from_record") as authenticate:
            result = org_migrate.migrate_one({"email": "one@example.invalid"}, code="invite", org_key="org")
        self.assertEqual(result["status"], "error")
        authenticate.assert_not_called()
        self.assertEqual(path.read_bytes(), b'{"org_keys":[]}')

    def test_invalid_migration_report_blocks_before_job_is_created(self):
        path = self.root / "migration.json"
        for raw in (b'{"private-canary":', b'null', b'[]', b'{"state":"running","state":"idle"}'):
            with self.subTest(raw=raw), patch("threading.Thread") as thread:
                path.write_bytes(raw)
                runner = OrgMigrationRunner(path)
                self.assertEqual(runner.snapshot()["state"], "needs_review")
                migrate = Mock()
                with self.assertRaises(JsonStateError):
                    runner.start(["one@example.invalid"], migrate)
                migrate.assert_not_called()
                thread.assert_not_called()
                self.assertEqual(path.read_bytes(), raw)

    def test_valid_interrupted_report_is_read_only_until_explicit_action(self):
        path = self.root / "migration.json"
        path.write_bytes(b'{"state":"running","total":1,"completed":0}')
        before = path.read_bytes()
        runner = OrgMigrationRunner(path)
        self.assertEqual(runner.snapshot()["state"], "interrupted")
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
