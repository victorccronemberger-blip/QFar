"""Offline App Check contract/regression tests; no installation cache or network."""
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, PropertyMock, patch

from moneymin import appcheck as a, minute_api as api


class AppCheckTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "FIREBASE_APP_CHECK_PROJECT_ID": "test-project",
            "FIREBASE_APP_CHECK_APP_ID": "1:123:android:abc",
            "FIREBASE_APP_CHECK_API_KEY": "fixture-key",
            "FIREBASE_APP_CHECK_DEBUG_TOKEN": "fixture-secret"})
        self.env.start()
        a.clear_cache()
        self.addCleanup(self.env.stop)
        self.addCleanup(a.clear_cache)
        self.http = patch.object(a.tls, 'urlopen')
        self.open = self.http.start()
        self.addCleanup(self.http.stop)
        self.response = self.open.return_value.__enter__.return_value
        self.response.status = 200
        self.response.read.return_value = json.dumps({'token': 'fixture.jwt.token', 'ttl': '60s'}).encode()

    def test_http_contract(self):
        self.assertEqual(a.get_app_check_header(), {'X-Firebase-AppCheck': 'fixture.jwt.token'})
        req = self.open.call_args.args[0]
        self.assertEqual(req.full_url, 'https://firebaseappcheck.googleapis.com/v1/projects/test-project/apps/1%3A123%3Aandroid%3Aabc:exchangeDebugToken?key=fixture-key')
        self.assertEqual(req.method, 'POST')
        self.assertEqual(json.loads(req.data), {'debugToken': 'fixture-secret'})
        self.assertEqual(self.open.call_args.kwargs['timeout'], 10)

    def test_absent_or_partial_configuration_is_quiet(self):
        for key in ('FIREBASE_APP_CHECK_DEBUG_TOKEN', 'FIREBASE_APP_CHECK_APP_ID'):
            with patch.dict(os.environ, {key: ''}), patch.object(a.logger, 'warning') as warning:
                self.assertEqual(a.get_app_check_header(), {})
                self.assertFalse(a.is_app_check_configured())
                warning.assert_not_called()
        self.open.assert_not_called()

    def test_real_ttl_and_fractional_duration(self):
        self.response.read.return_value = b'{"token":"token","ttl":"60.5s"}'
        with patch.object(a.time, 'monotonic', return_value=100):
            a.mint_app_check_token()
            self.assertAlmostEqual(a._expires, 154.45)
        with patch.object(a.time, 'monotonic', return_value=154):
            a.mint_app_check_token()
            self.assertEqual(self.open.call_count, 1)
        with patch.object(a.time, 'monotonic', return_value=155):
            a.mint_app_check_token()
            self.assertEqual(self.open.call_count, 2)

    def test_invalid_payloads_are_not_cached(self):
        for payload in ({}, [], {'token': 'x', 'ttl': 'invalid'},
                        {'token': 'x', 'ttl': '0s'}, {'token': 'x\nBad: header', 'ttl': '60s'},
                        {'token': 123, 'ttl': '60s'}):
            with self.subTest(payload=payload):
                a.clear_cache()
                self.response.read.return_value = json.dumps(payload).encode()
                self.assertEqual(a.get_app_check_header(), {})
                self.assertIsNone(a._token)

    def test_http_error_backoff_and_no_secret_logging(self):
        self.open.side_effect = RuntimeError('fixture-secret fixture-key remote private body')
        with patch.object(a.time, 'monotonic', return_value=100), self.assertLogs(a.logger, 'WARNING') as log:
            self.assertIsNone(a.mint_app_check_token())
            self.assertIsNone(a.mint_app_check_token(force_refresh=True))
            self.assertEqual(self.open.call_count, 1)
            self.assertNotIn('fixture', ' '.join(log.output))
        with patch.object(a.time, 'monotonic', return_value=161):
            a.mint_app_check_token()
            self.assertEqual(self.open.call_count, 2)

    def test_http_failure_status(self):
        self.response.status = 403
        self.assertEqual(a.get_app_check_header(), {})
        self.assertIsNone(a._token)

    def test_latency_cannot_extend_token_lifetime(self):
        with patch.object(a.time, 'monotonic', side_effect=[100, 170, 170]):
            self.assertIsNone(a.mint_app_check_token())

    def test_concurrent_calls_exchange_only_once(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: a.mint_app_check_token(), range(20)))
        self.assertEqual(set(results), {'fixture.jwt.token'})
        self.assertEqual(self.open.call_count, 1)

    def test_configuration_and_installation_separate_cache(self):
        a.mint_app_check_token()
        with patch.dict(os.environ, {'FIREBASE_APP_CHECK_DEBUG_TOKEN': 'another-secret'}):
            a.mint_app_check_token()
        with patch.object(a.config, 'ROOT', Path('other-installation')):
            a.mint_app_check_token()
        self.assertEqual(self.open.call_count, 3)

    def test_forced_refresh_and_memory_only_clear(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'APPDATA': root}):
            sentinel = Path(root)/'QMoney'/'appcheck_token.json'
            sentinel.parent.mkdir()
            sentinel.write_text('existing-user-file')
            a.mint_app_check_token()
            a.mint_app_check_token(force_refresh=True)
            a.clear_cache()
            self.assertEqual(sentinel.read_text(), 'existing-user-file')
            self.assertEqual(self.open.call_count, 2)
            self.assertIsNone(a._token)

    def test_explicit_rejection_detection(self):
        self.assertTrue(a.is_app_check_rejection('{"detail":{"error":"appcheck_required"}}'))
        self.assertTrue(a.is_app_check_rejection('{"detail":"Invalid App Check token"}'))
        for body in ('{"detail":"Request cannot be completed."}', '{"detail":"User account is disabled."}', '401', 'invalid'):
            self.assertFalse(a.is_app_check_rejection(body))

    def test_both_session_methods_preserve_appcheck_rejection_without_relogin(self):
        for detailed in (False, True):
            with self.subTest(detailed=detailed), patch.object(api.Session, 'id_token', new_callable=PropertyMock, return_value='firebase-token'):
                session = api.Session({'email': 'fixture@example.invalid', 'idToken': 'fixture'})
                body = '{"detail":{"error":"appcheck_required"}}'
                result = api.HttpResponse(401, body, {}) if detailed else (401, body)
                target = '_request_detailed' if detailed else '_request'
                with patch.object(api, target, return_value=result) as request, patch.object(session, 'refresh') as refresh, patch.object(session, '_relogin') as relogin:
                    actual = (session.request_detailed if detailed else session.request)('GET', '/api/v1/users/me')
                    self.assertEqual(actual, result)
                    self.assertEqual(request.call_args.kwargs['headers']['X-Firebase-AppCheck'], 'fixture.jwt.token')
                    refresh.assert_not_called()
                    relogin.assert_not_called()

    def test_success_without_configuration_for_both_methods(self):
        with patch.dict(os.environ, {'FIREBASE_APP_CHECK_DEBUG_TOKEN': ''}), patch.object(api.Session, 'id_token', new_callable=PropertyMock, return_value='firebase-token'):
            for detailed in (False, True):
                target = '_request_detailed' if detailed else '_request'
                result = api.HttpResponse(200, '{}', {}) if detailed else (200, '{}')
                with patch.object(api, target, return_value=result) as request:
                    s = api.Session({'email': 'fixture@example.invalid', 'idToken':'fixture'})
                    self.assertEqual((s.request_detailed if detailed else s.request)('GET','/api/v1/users/me'),result)
                    self.assertNotIn('X-Firebase-AppCheck',request.call_args.kwargs['headers'])
        self.open.assert_not_called()


if __name__ == '__main__':
    unittest.main()
