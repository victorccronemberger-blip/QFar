import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from moneymin.web import banned_monitor as monitor, server


class BannedMonitorTests(unittest.TestCase):
    def test_thread_start_failure_allows_retry(self):
        runner = monitor.BannedMonitor()
        with patch.object(monitor.threading.Thread, "start", side_effect=RuntimeError("private diagnostic")):
            with self.assertRaisesRegex(RuntimeError, "Não foi possível iniciar"):
                runner.start([{"email": "test@example.invalid"}], Mock())
        self.assertEqual(runner.snapshot()["state"], "error")
        self.assertNotIn("private diagnostic", str(runner.snapshot()))
        with patch.object(monitor.threading.Thread, "start") as start:
            runner.start([], Mock())
        start.assert_called_once()

    def profile_responses(self, disabled=False, state='active'):
        return [(200, json.dumps({'idToken': 'private-token'})),
                (200, json.dumps({'disabled': disabled, 'organizations': [
                    {'resourceKey': monitor.config.ORG_KEY, 'disabled': False}]})),
                (200, json.dumps({'userState': state}))]

    def test_readonly_status_does_not_save_tokens_join_or_restore_account(self):
        with patch.object(monitor.minute_api, '_request', side_effect=self.profile_responses()) as request, \
             patch.object(monitor.minute_api, 'login', side_effect=AssertionError('must not persist login')), \
             patch.object(monitor.minute_api, 'save_json', side_effect=AssertionError('must not save token')):
            result = monitor.check_status('banned@example.com', 'password')
        self.assertEqual(result['status'], 'unbanned')
        self.assertEqual(request.call_count, 3)
        self.assertTrue(all('/join' not in call.args[0] for call in request.call_args_list))

    def test_explicit_suspension_and_disabled_are_distinct_from_unknown(self):
        for disabled, state, expected in [(True, 'active', 'banned'), (False, 'on_hold', 'banned'),
                                         (False, 'inactive', 'banned'), (False, 'unknown', 'inconclusive')]:
            with self.subTest(state=state, disabled=disabled), \
                 patch.object(monitor.minute_api, '_request', side_effect=self.profile_responses(disabled, state)):
                self.assertEqual(monitor.check_status('banned@example.com', 'password')['status'], expected)

    def test_missing_password_never_contacts_services(self):
        with patch.object(monitor, 'check_status') as status, patch.object(monitor.crowtado, 'login') as login:
            result = monitor.inspect_account({'email': 'banned@example.com'})
        self.assertEqual(result['status'], 'missing_password')
        status.assert_not_called()
        login.assert_not_called()

    def test_balance_success_does_not_override_inconclusive_status(self):
        summary = {'availableCents': 123, 'pendingCents': 456, 'inTransitCents': 0, 'lifetimeCents': 789}
        with patch.object(monitor, 'check_status', side_effect=RuntimeError('HTTP 403 private-token')), \
             patch.object(monitor.crowtado, 'login', return_value=Mock()), \
             patch.object(monitor.crowtado, '_site_trpc', return_value=summary):
            result = monitor.inspect_account({'email': 'banned@example.com', 'password': 'private-password'})
        self.assertEqual(result['status'], 'inconclusive')
        self.assertEqual(result['balance']['availableCents'], 123)
        self.assertNotIn('private', json.dumps(result))

    def test_balance_failure_retains_previous_amount_and_date(self):
        row = {'email': 'banned@example.com', 'password': 'pw', 'monitor': {
            'balance': {'availableCents': 99}, 'balance_updated_at': 'previous-time'}}
        with patch.object(monitor, 'check_status', return_value={'status': 'banned'}), \
             patch.object(monitor.crowtado, 'login', side_effect=TimeoutError('timeout')):
            result = monitor.inspect_account(row)
        self.assertEqual(result['balance']['availableCents'], 99)
        self.assertEqual(result['balance_updated_at'], 'previous-time')
        self.assertTrue(result['balance_stale'])

    def test_claru_never_uses_crowtado_balance(self):
        with patch.object(monitor, 'check_status', return_value={'status': 'unbanned'}), \
             patch.object(monitor.crowtado, 'login') as login:
            result = monitor.inspect_account({'email': 'test@supply.claru.ai', 'password': 'pw'})
        self.assertEqual(result['balance_status'], 'not_applicable')
        login.assert_not_called()

    def test_monitor_endpoint_hides_password_and_refresh_rejects_overlap(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server.config, 'DATA_DIR', Path(tmp)), \
             patch.object(server, 'RUNNER', Mock()), \
             patch.object(server, 'ORG_MIGRATION', Mock(running=False)):
            (Path(tmp) / 'banned_accounts.json').write_text(json.dumps({'schema': 1, 'accounts': [
                {'email': 'banned@example.com', 'password': 'private-password', 'banned_at': 'yesterday'}]}))
            client = server.create_app().test_client()
            snapshot = client.get('/api/accounts/banned/monitor').get_json()
            self.assertNotIn('private-password', json.dumps(snapshot))
            self.assertTrue(snapshot['accounts'][0]['has_password'])
            with patch.object(monitor.threading.Thread, 'start'):
                self.assertEqual(client.post('/api/accounts/banned/refresh').status_code, 202)
                self.assertEqual(client.post('/api/accounts/banned/refresh').status_code, 409)

    def test_runner_persists_each_result_and_completes(self):
        runner, save = monitor.BannedMonitor(), Mock()
        runner.state = {'state': 'running', 'completed': 0, 'total': 1}
        with patch.object(monitor, 'inspect_account', return_value={'status': 'unbanned'}):
            runner._run([{'email': 'banned@example.com'}], save)
        save.assert_called_once_with('banned@example.com', {'status': 'unbanned'})
        self.assertEqual(runner.snapshot()['state'], 'completed')
