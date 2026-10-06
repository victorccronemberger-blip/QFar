import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from moneymin.web import server


class LocalMediaLibraryApiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.media = Path(temporary.name) / 'data'
        configuration = patch.object(server.config, 'MEDIA_DATA_DIR', self.media)
        configuration.start()
        self.addCleanup(configuration.stop)
        self.workers = []
        thread_factory = threading.Thread

        def track(*args, **kwargs):
            thread = thread_factory(*args, **kwargs)
            if thread.name == 'qmoney-task-catalog':
                self.workers.append(thread)
            return thread

        worker_patch = patch('moneymin.web.catalog_loader.threading.Thread', side_effect=track)
        worker_patch.start()
        self.addCleanup(worker_patch.stop)
        self.addCleanup(self.join_workers)
        self.client = server.create_app(for_testing=True).test_client()

    def join_workers(self):
        for worker in self.workers:
            if worker.ident is not None:
                worker.join(10)
                self.assertFalse(worker.is_alive())

    def test_sources_without_native_are_visible_and_no_account_or_download_is_needed(self):
        ego = self.media / 'ego4d'
        ego.mkdir(parents=True)
        (ego / 'downloaded.mp4').write_bytes(b'local media bytes')
        (ego / 'downloaded_imu.csv').write_text('timestamp,ax,ay,az,gx,gy,gz\n')
        with patch.object(server.Session, 'from_email') as account, \
                patch.object(server.ego4d, '_s3') as download, \
                patch.object(server.campaign, 'prepare_clip') as prepare:
            result = self.client.get('/api/storage/library/items')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['inventory_scope'], 'local_media_files')
        self.assertEqual(result.json['total'], 2)
        self.assertEqual({row['kind'] for row in result.json['items']}, {'video', 'sensor'})
        self.assertGreater(result.json['disk_free_bytes'], 0)
        account.assert_not_called()
        download.assert_not_called()
        prepare.assert_not_called()

    def test_invalid_filters_are_rejected_before_enumeration(self):
        for query in ('provider=minute', 'kind=accepted', 'limit=0', 'limit=101',
                      'offset=-1', 'offset=100001', 'q=' + 'x' * 201,
                      'refresh=' + 'x' * 81):
            with self.subTest(query=query), \
                    patch.object(server.prepared_library, 'list_local_media') as listing:
                response = self.client.get('/api/storage/library/items?async=1&' + query)
                self.assertEqual(response.status_code, 400)
                listing.assert_not_called()

    def test_cold_enumeration_runs_once_in_worker_and_refresh_is_idempotent(self):
        entered, release = threading.Event(), threading.Event()
        called_on = []

        def slow(*args, **kwargs):
            called_on.append(threading.current_thread().name)
            entered.set()
            if not release.wait(10):
                raise AssertionError('worker was not released')
            return {'items': [], 'total': 0}

        path = '/api/storage/library/items?async=1&provider=holoassist&kind=video&q=clip'
        with patch.object(server.prepared_library, 'list_local_media', side_effect=slow) as listing:
            try:
                self.assertEqual(self.client.get(path).status_code, 202)
                self.assertTrue(entered.wait(5))
                started = time.monotonic()
                for _ in range(5):
                    self.assertEqual(self.client.get(path).status_code, 202)
                self.assertLess(time.monotonic() - started, .5)
                self.assertEqual(listing.call_count, 1)
            finally:
                release.set()
                self.join_workers()
            self.assertEqual(self.client.get(path).status_code, 200)
            self.assertEqual(called_on, ['qmoney-task-catalog'])
            self.assertEqual(listing.call_args.kwargs['provider'], 'holoassist')
            refreshed = path + '&refresh=click-1'
            self.client.get(refreshed)
            self.join_workers()
            self.assertEqual(self.client.get(refreshed).status_code, 200)
            self.assertEqual(self.client.get(refreshed).status_code, 200)
            self.assertEqual(listing.call_count, 2)

    def test_empty_root_stays_absent_and_errors_hide_private_paths(self):
        response = self.client.get('/api/storage/library/items')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['items'], [])
        self.assertFalse(self.media.exists())
        with patch.object(server.prepared_library, 'list_local_media',
                          side_effect=OSError('private-account-folder')):
            response = self.client.get('/api/storage/library/items')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private-account-folder', response.get_data(as_text=True))
