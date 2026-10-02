import json
from contextlib import closing
import sqlite3
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from moneymin import ego4d_library
from moneymin.web import server
import test_ego4d_library


class OriginalLibraryApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.media = Path(self.temporary.name)
        self.root = self.media / 'ego4d'
        self.root.mkdir()
        test_ego4d_library.OriginalLibraryTests().source(self.root)
        self.config_patch = patch.object(server.config, 'MEDIA_DATA_DIR', self.media)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.client = server.create_app().test_client()

    def index(self):
        return ego4d_library.index_library(self.root, self.root / 'library.sqlite3')

    def test_missing_index_is_explicit_and_never_calls_account_or_remote_api(self):
        with patch.object(server.Session, 'from_email') as session, patch.object(server.ego4d, '_s3') as s3:
            self.assertEqual(self.client.get('/api/library/ego4d').json['state'], 'missing')
            self.assertEqual(self.client.get('/api/library/ego4d/videos').status_code, 409)
            session.assert_not_called()
            s3.assert_not_called()

    def test_complete_browse_filters_unknown_sensors_without_asserting_approval(self):
        self.index()
        result = self.client.get('/api/library/ego4d/videos?q=soup&min_s=300').json
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['uid'], 'v1')
        self.assertEqual(result['provenance'], 'ego4d_original')
        self.assertFalse(result['items'][0]['imu_local'])
        self.assertEqual(self.client.get('/api/library/ego4d/videos?imu=declared').json['total'], 0)
        summary = self.client.get('/api/library/ego4d').json
        self.assertEqual(summary['videos_with_unknown_imu'], 1)
        self.assertEqual(summary['sensor_verification'], 'not_implied_by_presence')

    def test_invalid_filters_fail_without_downloading_or_starting_campaign(self):
        self.index()
        for query in ('min_s=nan', 'max_s=inf', 'min_s=20&max_s=10', 'offset=-1', 'limit=0',
                      'imu=true', 'q=' + 'x' * 201):
            with self.subTest(query=query):
                self.assertEqual(self.client.get('/api/library/ego4d/videos?' + query).status_code, 400)

    def test_source_change_requires_reindex_instead_of_serving_stale_results(self):
        self.index()
        with (self.root / 'clips.csv').open('a') as stream:
            stream.write('\n')
        self.assertTrue(self.client.get('/api/library/ego4d').json['needs_index'])
        self.assertEqual(self.client.get('/api/library/ego4d/videos').status_code, 409)

    def test_incomplete_index_is_never_reported_ready(self):
        for damage in ('DELETE FROM index_report',
                       "UPDATE index_report SET json='{}'",
                       "DELETE FROM source_file WHERE name='clips.csv'"):
            with self.subTest(damage=damage):
                self.index()
                with closing(sqlite3.connect(self.root / 'library.sqlite3')) as db:
                    db.execute(damage)
                    db.commit()
                summary = self.client.get('/api/library/ego4d').json
                self.assertEqual(summary['state'], 'corrupt')
                self.assertTrue(summary['needs_index'])
                self.assertNotEqual(self.client.get('/api/library/ego4d/videos').status_code, 200)

    def test_background_index_is_idempotent_and_http_does_not_block(self):
        entered, release = threading.Event(), threading.Event()
        real = ego4d_library.index_library
        def delayed(*args, **kwargs):
            entered.set()
            release.wait(3)
            return real(*args, **kwargs)
        with patch.object(ego4d_library, 'index_library', side_effect=delayed) as build:
            try:
                self.assertEqual(self.client.post('/api/library/ego4d/index').status_code, 202)
                self.assertTrue(entered.wait(1))
                started = time.monotonic()
                for _ in range(5):
                    self.assertEqual(self.client.post('/api/library/ego4d/index').status_code, 202)
                self.assertLess(time.monotonic() - started, .5)
                self.assertEqual(build.call_count, 1)
            finally:
                release.set()
            for _ in range(100):
                response = self.client.post('/api/library/ego4d/index')
                if response.status_code != 202:
                    break
                time.sleep(.01)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.client.get('/api/library/ego4d/videos').json['total'], 2)
            self.assertEqual(build.call_count, 1)

    def test_failed_build_keeps_original_files_and_reports_recoverable_error(self):
        original = (self.root / 'ego4d.json').read_bytes()
        (self.root / 'timed_narrations.jsonl').write_text('broken-json')
        for _ in range(100):
            response = self.client.post('/api/library/ego4d/index')
            if response.status_code != 202:
                break
            time.sleep(.01)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(original, (self.root / 'ego4d.json').read_bytes())
        self.assertFalse((self.root / 'library.sqlite3').exists())
