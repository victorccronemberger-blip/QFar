import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from moneymin.web import server
from moneymin.web.runner import HoloCacheRunner


class PreparedLibraryApiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.media = Path(temporary.name)
        self.config_patch = patch.object(server.config, 'MEDIA_DATA_DIR', self.media)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.workers = []
        real_thread = threading.Thread

        def track(*args, **kwargs):
            worker = real_thread(*args, **kwargs)
            if worker.name == 'qmoney-task-catalog':
                self.workers.append(worker)
            return worker

        thread_patch = patch('moneymin.web.catalog_loader.threading.Thread', side_effect=track)
        thread_patch.start()
        self.addCleanup(thread_patch.stop)
        self.addCleanup(self.join_workers)
        self.client = server.create_app(for_testing=True).test_client()

    def join_workers(self):
        for worker in self.workers:
            if worker.ident is not None:
                worker.join(10)
                self.assertFalse(worker.is_alive())

    def await_response(self, path):
        self.join_workers()
        return self.client.get(path)

    def test_empty_library_is_read_only_without_account_or_download(self):
        with patch.object(server.Session, 'from_email') as account, \
                patch.object(server.ego4d, '_s3') as download, \
                patch.object(server.campaign, 'prepare_clip') as prepare:
            response = self.client.get('/api/library/ego4d/prepared')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['items'], [])
        self.assertFalse((self.media / 'ego4d').exists())
        account.assert_not_called()
        download.assert_not_called()
        prepare.assert_not_called()

    def test_invalid_filters_stop_before_inventory_work(self):
        for query in ('state=approved', 'min_s=nan', 'min_s=-1', 'max_s=inf',
                      'min_s=100&max_s=50', 'limit=0', 'limit=101', 'offset=-1', 'offset=100001',
                      'q=' + 'x' * 201, 'refresh=' + 'x' * 81):
            with self.subTest(query=query), \
                    patch.object(server.prepared_library, 'list_prepared_clips') as listing, \
                    patch.object(server.prepared_library, 'inventory_signature') as signature:
                response = self.client.get('/api/library/ego4d/prepared?async=1&' + query)
                self.assertEqual(response.status_code, 400)
                listing.assert_not_called()
                signature.assert_not_called()

    def test_background_read_is_shared_and_refresh_nonce_is_idempotent(self):
        entered, release = threading.Event(), threading.Event()

        def slow(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise AssertionError('worker not released')
            return {'items': [], 'total': 0, 'counts': {'ready': 0}}

        path = '/api/library/ego4d/prepared?async=1&q=car&state=ready&min_s=180&max_s=780'
        with patch.object(server.prepared_library, 'inventory_signature', return_value=('v1',)), \
                patch.object(server.prepared_library, 'list_prepared_clips', side_effect=slow) as listing:
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
            self.assertEqual(listing.call_args.kwargs['minimum_s'], 180)
            self.assertEqual(listing.call_args.kwargs['maximum_s'], 780)
            self.assertFalse(listing.call_args.kwargs['force_refresh'])
            refresh_path = path + '&refresh=click-1'
            self.client.get(refresh_path)
            self.assertEqual(self.await_response(refresh_path).status_code, 200)
            self.assertEqual(self.client.get(refresh_path).status_code, 200)
            self.assertEqual(listing.call_count, 2)
            self.assertTrue(listing.call_args.kwargs['force_refresh'])

    def test_file_signature_change_replaces_cached_result(self):
        signature = ['first']
        path = '/api/library/ego4d/prepared?async=1'
        with patch.object(server.prepared_library, 'inventory_signature', side_effect=lambda root: tuple(signature)), \
                patch.object(server.prepared_library, 'list_prepared_clips', side_effect=[
                    {'items': [], 'total': 0}, {'items': [{'clip_uid': 'new'}], 'total': 1}]) as listing:
            self.client.get(path)
            self.assertEqual(self.await_response(path).json['total'], 0)
            signature[0] = 'second'
            self.client.get(path)
            self.assertEqual(self.await_response(path).json['total'], 1)
            self.assertEqual(listing.call_count, 2)

    def test_read_failure_has_no_private_path_in_error(self):
        with patch.object(server.prepared_library, 'list_prepared_clips',
                          side_effect=OSError('private-account-and-path')):
            response = self.client.get('/api/library/ego4d/prepared')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private-account-and-path', response.get_data(as_text=True))


class LibraryPreparationDurationTests(unittest.TestCase):
    def setUp(self):
        self.client = server.create_app(for_testing=True).test_client()

    def test_get_and_start_pass_requested_duration_to_existing_pipeline(self):
        with patch.object(server.ego_accelerator, 'cache_status', return_value={'ready': 0}) as status, \
                patch.object(server, 'load_json', return_value={}):
            response = self.client.get('/api/holo-cache?provider=ego4d&min_dur_s=180&max_dur_s=780')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(status.call_args.kwargs['min_dur_s'], 180)
        self.assertEqual(status.call_args.kwargs['max_dur_s'], 780)
        with patch.object(server.ego_accelerator, 'catalog_installed', return_value=True), \
                patch.object(server.ego_accelerator, 'storage_limits', return_value={'budget_gb': 10}), \
                patch.object(server, 'HOLO_CACHE_RUNNER', Mock(running=False)) as runner, \
                patch.object(server, 'RUNNER', Mock(running=False)), \
                patch.object(server, 'RECOVERY', Mock(running=False)):
            runner.snapshot.return_value = {}
            response = self.client.post('/api/holo-cache/start', json={
                'provider': 'ego4d', 'budget_gb': 10, 'min_dur_s': 180, 'max_dur_s': 780})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(runner.start.call_args.kwargs['min_dur_s'], 180)
        self.assertEqual(runner.start.call_args.kwargs['max_dur_s'], 780)

    def test_invalid_duration_never_starts_preparation(self):
        for low, high in ((59, 1800), (60, 1801), (800, 700), ('nan', 800),
                          (60, 'inf'), (True, 800), (60, None)):
            with self.subTest(low=low, high=high), \
                    patch.object(server, 'HOLO_CACHE_RUNNER') as runner, \
                    patch.object(server.ego_accelerator, 'storage_limits') as storage:
                response = self.client.post('/api/holo-cache/start', json={
                    'provider': 'ego4d', 'min_dur_s': low, 'max_dur_s': high})
                self.assertEqual(response.status_code, 400)
                runner.start.assert_not_called()
                storage.assert_not_called()

    def test_runner_exposes_preserved_count_without_marking_ready(self):
        runner = HoloCacheRunner()

        def warm(**kwargs):
            self.assertEqual(kwargs['min_dur_s'], 180)
            self.assertEqual(kwargs['max_dur_s'], 780)
            kwargs['progress']('protected', {'video_name': 'local.mp4'})
            self.assertEqual(runner.snapshot()['protected'], 1)
            self.assertEqual(runner.snapshot()['ready'], 0)
            return {'status': 'complete', 'ready': 0, 'failed': 0, 'protected': 1}

        with patch.object(server.ego_accelerator, 'warm_cache', side_effect=warm):
            runner.start(provider='ego4d', task='Furniture Assembly', min_dur_s=180, max_dur_s=780)
            runner._thread.join(5)
        self.assertFalse(runner._thread.is_alive())
        self.assertEqual(runner.snapshot()['protected'], 1)
        self.assertEqual(runner.snapshot()['ready'], 0)
        self.assertEqual(runner.snapshot()['state'], 'done')
