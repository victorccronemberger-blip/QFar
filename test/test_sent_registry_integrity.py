import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from moneymin import config, sent_registry


class SentRegistryIntegrityTests(unittest.TestCase):
    def test_skipped_and_unfinalized_accounts_are_not_reconstructed_as_sent(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "DATA_DIR", Path(directory)):
            (Path(directory) / "campaign_old.json").write_text(json.dumps({"items": [{
                "clip_uid": "clip", "registry_key": "original-key", "task_scenario": "translated",
                "accounts": [{"email": "skipped", "ok": True, "skipped": True},
                             {"email": "pending", "ok": True, "finalized": False},
                             {"email": "confirmed", "ok": True, "finalized": True}]}]}))
            self.assertEqual(sent_registry.sent_emails("original-key", "clip"), {"confirmed"})
            self.assertFalse(sent_registry.sent_emails("translated", "clip"))

    def test_reset_survives_missing_registry_without_resurrecting_history(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "DATA_DIR", Path(directory)):
            def history(name, clip):
                (Path(directory) / name).write_text(json.dumps({"items": [{
                    "clip_uid": clip, "task_scenario": "task", "accounts": [{"email": "a", "ok": True}]}]}))
            history("campaign_old.json", "old")
            self.assertEqual(sent_registry.sent_emails("task", "old"), {"a"})
            sent_registry.reset()
            (Path(directory) / sent_registry.FILE_NAME).unlink()
            history("campaign_new.json", "new")
            self.assertFalse(sent_registry.sent_emails("task", "old"))
            self.assertEqual(sent_registry.sent_emails("task", "new"), {"a"})

    def test_corrupt_registry_never_becomes_empty_or_overwritten(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "DATA_DIR", Path(directory)):
            path = Path(directory) / sent_registry.FILE_NAME
            for payload in (b"broken", b"[]", b'{"task":{"clip":null}}', b'{"task":{"clip":[12]}}'):
                with self.subTest(payload=payload):
                    path.write_bytes(payload)
                    with self.assertRaises(ValueError):
                        sent_registry.sent_emails("task", "clip")
                    with self.assertRaises(ValueError):
                        sent_registry.mark_sent("task", "other", "a@example.com")
                    self.assertEqual(path.read_bytes(), payload)

    def test_concurrent_initial_reads_and_writes_preserve_every_account(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "DATA_DIR", Path(directory)):
            (Path(directory) / "campaign_old.json").write_text(json.dumps({"items": [{
                "clip_uid": "clip", "task_scenario": "task", "accounts": [{"email": "old", "ok": True}]
            }]}))
            def operation(index):
                sent_registry.load()
                sent_registry.mark_sent("task", "clip", f"account-{index}")
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(operation, range(30)))
            self.assertEqual(sent_registry.sent_emails("task", "clip"),
                             {"old"} | {f"account-{index}" for index in range(30)})

    def test_malformed_history_rows_do_not_crash_reconstruction(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "DATA_DIR", Path(directory)):
            for index, items in enumerate((None, [None], [{"clip_uid": "x", "accounts": None}],
                                          [{"clip_uid": "x", "accounts": [None]}],
                                          [{"clip_uid": {}, "accounts": [{"ok": True, "email": "a"}]}],
                                          [{"clip_uid": "x", "task_scenario": [1]}])):
                (Path(directory) / f"campaign_{index}.json").write_text(json.dumps({"items": items}))
            self.assertEqual(sent_registry.load(), {})
