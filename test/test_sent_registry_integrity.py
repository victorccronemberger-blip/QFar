import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from moneymin import config, sent_registry


class SentRegistryIntegrityTests(unittest.TestCase):
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
