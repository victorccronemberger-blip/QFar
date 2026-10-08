import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from moneymin import campaign_types
from moneymin.campaign_types import CampaignLog


class CampaignLogConcurrencyTests(unittest.TestCase):
    def test_save_retries_only_a_transient_windows_sharing_violation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "campaign.json"
            log = CampaignLog("fixture-time", [])
            sharing = PermissionError(13, "sharing violation")
            sharing.winerror = 32
            with patch.object(campaign_types, "save_json", side_effect=[sharing, None]) as save:
                self.assertEqual(log.save(path), path)
            self.assertEqual(save.call_count, 2)

            denied = PermissionError(13, "access denied")
            denied.winerror = 5
            with patch.object(campaign_types, "save_json", side_effect=denied) as save:
                with self.assertRaises(PermissionError):
                    log.save(path)
            save.assert_called_once()

    def test_parallel_saves_share_one_locked_snapshot_and_keep_every_item(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "campaign.json"
            count = 8
            log = CampaignLog("fixture-time", [f"worker-{i}@example.invalid" for i in range(count)])
            for index in range(count):
                log.add_item({"clip_uid": f"clip-{index}", "accounts": [{"worker": index}]})

            start = threading.Barrier(count)
            attempted = [threading.Event() for _ in range(count)]
            first_save_entered = threading.Event()
            release_first_save = threading.Event()
            counts_lock = threading.Lock()
            active = 0
            max_active = 0
            original_save_json = campaign_types.save_json

            def slow_save_json(destination, payload):
                nonlocal active, max_active
                with counts_lock:
                    active += 1
                    max_active = max(max_active, active)
                    first = active == 1
                try:
                    if first:
                        first_save_entered.set()
                        if not release_first_save.wait(5):
                            raise AssertionError("test did not release the first save")
                    return original_save_json(destination, payload)
                finally:
                    with counts_lock:
                        active -= 1

            def write(index):
                start.wait(timeout=5)
                attempted[index].set()
                return log.save(path)

            with patch.object(campaign_types, "save_json", side_effect=slow_save_json):
                try:
                    with ThreadPoolExecutor(max_workers=count) as pool:
                        futures = [pool.submit(write, index) for index in range(count)]
                        self.assertTrue(first_save_entered.wait(5))
                        self.assertTrue(all(event.wait(5) for event in attempted))
                        self.assertFalse(log._save_lock.acquire(blocking=False))
                        release_first_save.set()
                        self.assertEqual([future.result(timeout=5) for future in futures], [path] * count)
                finally:
                    release_first_save.set()

            self.assertEqual(max_active, 1)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual({item["clip_uid"] for item in payload["items"]},
                             {f"clip-{index}" for index in range(count)})
            self.assertNotIn("_save_lock", log.to_dict())


if __name__ == "__main__":
    unittest.main()
