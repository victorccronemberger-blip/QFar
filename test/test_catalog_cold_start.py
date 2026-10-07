import json
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from moneymin import campaign, ego4d
from moneymin.web import server


class PreparedCatalogTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(campaign.config, "DATA_DIR", self.root))
        self.stack.enter_context(patch.object(campaign.config, "MEDIA_DATA_DIR", self.root / "library"))
        self.stack.enter_context(patch.object(ego4d, "EGO4D_DIR", self.root / "library" / "ego4d"))
        self.stack.enter_context(patch.object(ego4d, "_aws_creds", side_effect=RuntimeError("not configured")))
        # A public, unconfigured install can inspect the seed, but cannot count
        # those suggestions as current activity/sensor evidence for a campaign.
        for func in (campaign._rank_cache_stamp, campaign._ranked_pools_cached,
                     campaign._duration_ranked_pools):
            func.cache_clear()
            self.addCleanup(func.cache_clear)
        campaign._load_rank_seed.cache_clear()
        self.addCleanup(campaign._load_rank_seed.cache_clear)
        names = json.loads((Path(__file__).parent / "current_minute_task_names.json").read_text())
        self.tasks = [{"id": str(i), "name": name} for i, name in enumerate(names)]

    def test_first_load_and_duration_changes_do_not_admit_unproven_portable_index(self):
        for minimum, maximum in ((300, 1800), (60, 600), (600, 1200), (300, 1800)):
            with self.subTest(duration=(minimum, maximum)):
                rows = campaign.available_tasks("fixture@example.invalid", "org", remote_tasks=self.tasks,
                    min_dur_s=minimum, max_dur_s=maximum, dataset_provider="ego4d",
                    include_unavailable=True)
                self.assertEqual(len(rows), len(self.tasks))
                self.assertFalse(any(r["clip_count"] > 0 for r in rows))
                self.assertTrue(all('Integrações' in r['unavailable_reason']
                                    for r in rows if r['mapping_supported']))
                for row in rows:
                    if row.get("dur_range_s"):
                        self.assertGreaterEqual(row["dur_range_s"][0], minimum)
                        self.assertLessEqual(row["dur_range_s"][1], maximum)

    def test_stale_cache_after_update_is_replaced_by_prepared_index(self):
        import pickle
        campaign._rank_cache_path().write_bytes(pickle.dumps(((), {"Shopping": []})))
        pools = campaign._ranked_pools()
        self.assertTrue(pools["Shopping"])
        self.assertIsNotNone(campaign._load_rank_cache())

    def test_new_supported_activity_without_portable_content_does_not_download_catalog(self):
        with patch.object(campaign, '_task_candidates',
                          side_effect=AssertionError('cold task lookup must not acquire sources')):
            for minimum, maximum in ((300, 1800), (60, 600), (600, 1200)):
                rows = campaign.available_tasks('fixture@example.invalid', 'org',
                    remote_tasks=[{'id': 'new-coffee', 'name': 'Brew Coffee or Tea'}],
                    min_dur_s=minimum, max_dur_s=maximum,
                    dataset_provider='ego4d', include_unavailable=True)
                self.assertTrue(rows[0]['mapping_supported'])
                self.assertEqual(rows[0]['clip_count'], 0)
                self.assertFalse(rows[0]['available_for_duration'])

    def test_async_http_returns_categories_after_duration_changes(self):
        session = Mock()
        session.all_tasks.return_value = self.tasks
        with patch.object(server.Session, "from_email", return_value=session), \
             patch.object(server, "_resolve_org", return_value="org"):
            client = server.create_app(for_testing=True).test_client()
            for minimum, maximum in ((300, 1800), (60, 600), (600, 1200)):
                path = ("/api/tasks?async=1&email=fixture@example.invalid&dataset=ego4d"
                        f"&min_dur_s={minimum}&max_dur_s={maximum}")
                deadline = time.monotonic() + 5
                while True:
                    response = client.get(path)
                    if response.status_code != 202:
                        break
                    if time.monotonic() >= deadline:
                        self.fail("prepared catalog did not finish")
                    time.sleep(.01)
                self.assertEqual(response.status_code, 200, response.json)
                self.assertFalse(any(row["clip_count"] for row in response.json["tasks"]))
