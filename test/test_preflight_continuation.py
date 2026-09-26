import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from moneymin.minute_api import AuthError
from moneymin import credential_store
from moneymin.web import server


class PreflightContinuationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.secrets = self.root / 'secrets'
        self.secrets.mkdir()
        for name, value in [('DATA_DIR', self.root), ('SECRETS_DIR', self.secrets), ('ROOT', self.root), ('LIBRARY_ROOT', self.root)]:
            self.stack.enter_context(patch.object(server.config, name, value))
        for name, filename in [('PREFS_PATH', 'prefs.json'), ('BALANCES_PATH', 'balances.json'),
                               ('CROWTADO_PW_PATH', 'passwords.json'), ('ACCOUNT_HEALTH_PATH', 'health.json')]:
            self.stack.enter_context(patch.object(server, name, self.root / filename))
        self.stack.enter_context(patch.object(server.crowtado, 'clear_cached_session'))
        self.runner = Mock(running=False, total_sends=1)
        self.stack.enter_context(patch.object(server, 'RUNNER', self.runner))
        for name in ['HOLO_CACHE_RUNNER', 'BALANCES_RUNNER', 'ORG_MIGRATION']:
            self.stack.enter_context(patch.object(server, name, Mock(running=False)))
        for email in ['good@example.com', 'bad@example.com']:
            server.config.token_path(email).write_text(json.dumps({'email': email, 'idToken': 'secret', 'refreshToken': 'private'}))
        self.body = {'accounts': ['good@example.com', 'bad@example.com'],
                     'tasks': [{'task_id': 'task'}], 'dataset': 'ego4d'}
        server.CROWTADO_PW_PATH.write_text(json.dumps({'bad@example.com': 'saved-password', 'good@example.com': 'healthy-password'}))
        self.failure = AuthError('disabled', code='restricted')
        def resolve(email):
            if email.startswith('bad'): raise self.failure
            return server.config.ORG_KEY
        self.resolve = self.stack.enter_context(patch.object(server, '_resolve_org', side_effect=resolve))
        self.catalog = self.stack.enter_context(patch.object(server.campaign, 'available_tasks', return_value=[{
            'id': 'task', 'name': 'Task', 'scenario': 'task', 'clip_count': 1, 'available_for_duration': True}]))
        self.ready = self.stack.enter_context(patch.object(server.readiness, 'campaign_readiness', return_value={'ready': True, 'checks': []}))
        self.stack.enter_context(patch.object(server, '_storage_snapshot', return_value={'free_bytes': 100 * 1024**3}))
        self.client = server.create_app().test_client()

    def preflight(self):
        response = self.client.post('/api/campaigns/preflight', json=self.body)
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def test_remove_continue_archives_password_and_date_without_tokens(self):
        result = self.preflight()
        self.assertTrue(result['can_remove_and_continue'])
        self.assertTrue(server.config.token_path('bad@example.com').exists())
        before = (self.resolve.call_count, self.catalog.call_count, self.ready.call_count)
        response = self.client.post('/api/campaigns', json={**self.body, 'preflight_id': result['preflight_id'], 'remove_restricted': True})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(before, (self.resolve.call_count, self.catalog.call_count, self.ready.call_count))
        self.assertEqual([a.email for a in self.runner.start.call_args.args[0].accounts], ['good@example.com'])
        self.assertFalse(server.config.token_path('bad@example.com').exists())
        self.assertIn('bad@example.com', server._removed_accounts())
        archive = self.client.get('/api/accounts/banned').get_json()
        self.assertEqual(archive['accounts'][0]['email'], 'bad@example.com')
        self.assertEqual(archive['accounts'][0]['password'], 'saved-password')
        self.assertTrue(archive['accounts'][0]['banned_at'])
        self.assertNotIn('bad@example.com', json.loads(server.CROWTADO_PW_PATH.read_text()))
        saved_date = archive['accounts'][0]['banned_at']
        server._ban_accounts([{'email': 'bad@example.com', 'restriction_confirmed': True}])
        archived_again = self.client.get('/api/accounts/banned').get_json()['accounts'][0]
        self.assertEqual(archived_again['password'], 'saved-password')
        self.assertEqual(archived_again['banned_at'], saved_date)
        self.assertNotIn('private', json.dumps(archive))
        self.assertNotIn('secret', json.dumps(archive))
        attempt = self.client.post('/api/accounts/import', json={'content': json.dumps([
            {'email': 'bad@example.com', 'password': 'old-backup-password'}]), 'apply': True})
        self.assertEqual(attempt.get_json()['counts']['invalid'], 1)
        self.assertFalse(server.config.token_path('bad@example.com').exists())
        replay = self.client.post('/api/campaigns', json={**self.body, 'preflight_id': result['preflight_id'], 'remove_restricted': True})
        self.assertEqual(replay.status_code, 409)

    def test_temporary_failure_never_offers_permanent_removal(self):
        self.failure = TimeoutError('timeout')
        result = self.preflight()
        self.assertFalse(result['can_remove_and_continue'])
        self.assertIsNone(result['preflight_id'])

    def test_modified_request_or_changed_token_rejects_without_removal(self):
        for mutation in ['body', 'token', 'expired']:
            with self.subTest(mutation=mutation):
                result = self.preflight()
                body = {**self.body, 'preflight_id': result['preflight_id'], 'remove_restricted': True}
                if mutation == 'body': body['count'] = 99
                if mutation == 'token': server.config.token_path('good@example.com').write_text(json.dumps({'email': 'good@example.com', 'idToken': 'changed'}))
                with patch.object(server.time, 'monotonic', return_value=10**15) if mutation == 'expired' else ExitStack():
                    response = self.client.post('/api/campaigns', json=body)
                self.assertEqual(response.status_code, 409)
                self.assertTrue(server.config.token_path('bad@example.com').exists())
        self.runner.start.assert_not_called()

    def test_background_token_rotation_does_not_invalidate_preflight(self):
        result = self.preflight()
        token_path = server.config.token_path('good@example.com')
        token = json.loads(token_path.read_text())
        token.update(idToken='renewed-token', refreshToken='rotated-refresh',
                     expiresIn='3600', expires_at=9999999999)
        token_path.write_text(json.dumps(token, indent=2))
        response = self.client.post('/api/campaigns', json={
            **self.body, 'preflight_id': result['preflight_id'], 'remove_restricted': True})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.runner.start.assert_called_once()

    def test_identity_change_still_invalidates_preflight(self):
        result = self.preflight()
        token_path = server.config.token_path('good@example.com')
        token = json.loads(token_path.read_text())
        token['localId'] = 'another-user'
        token_path.write_text(json.dumps(token))
        response = self.client.post('/api/campaigns', json={
            **self.body, 'preflight_id': result['preflight_id'], 'remove_restricted': True})
        self.assertEqual(response.status_code, 409)
        self.runner.start.assert_not_called()

    def test_ban_purges_legacy_credentials_backups_and_health_but_preserves_healthy(self):
        from moneymin import account_bans
        data = self.root / 'data'
        data.mkdir()
        records = [{'email': 'bad@example.com', 'password': 'old'},
                   {'email': 'good@example.com', 'password': 'good'}]
        (data / 'novas_contas_test.json').write_text(json.dumps(records))
        (data / 'contas.jsonl').write_text('\n'.join(json.dumps(r) for r in records))
        (data / 'account_health.json').write_text(json.dumps({r['email']: {'status': 'active'} for r in records}))
        credential_store.save(self.secrets, 'bad@example.com', 'new-bad-password')
        credential_store.save(self.secrets, 'good@example.com', 'new-good-password')
        recovery = self.root / 'recovery'
        recovery.mkdir()
        (recovery / 'old-backup.json').write_text(json.dumps({'accounts': records}))
        account_bans.purge_local_records({'bad@example.com'})
        for path in [data / 'novas_contas_test.json', data / 'contas.jsonl',
                     data / 'account_health.json', recovery / 'old-backup.json']:
            self.assertNotIn('bad@example.com', path.read_text())
            self.assertIn('good@example.com', path.read_text())
        self.assertIsNone(credential_store.lookup(self.secrets, 'bad@example.com'))
        self.assertEqual(
            credential_store.lookup(self.secrets, 'good@example.com'),
            'new-good-password',
        )

    def test_runtime_restriction_calls_permanent_removal_handler(self):
        from moneymin.web.runner import CampaignRunner
        runner = CampaignRunner()
        runner.on_restriction = Mock()
        runner._on_event('account_excluded', {'email': 'bad@example.com'})
        runner.on_restriction.assert_called_once_with('bad@example.com')
        self.assertTrue(runner.snapshot()['events'][-1]['permanently_removed'])

    def test_archive_failure_prevents_deletion_and_start(self):
        result = self.preflight()
        (self.root / 'banned_accounts.json').write_text('[]')
        response = self.client.post('/api/campaigns', json={**self.body, 'preflight_id': result['preflight_id'], 'remove_restricted': True})
        self.assertEqual(response.status_code, 500)
        self.assertTrue(server.config.token_path('bad@example.com').exists())
        self.runner.start.assert_not_called()

    def test_legacy_export_recovers_available_password_and_marks_missing_as_null(self):
        path = self.root / 'banned_accounts.json'
        path.write_text(json.dumps({'schema': 1, 'accounts': [
            {'email': 'bad@example.com', 'removed_at': '2026-09-17T10:00:00-0300'},
            {'email': 'missing@example.com', 'removed_at': '2026-09-16T10:00:00-0300'}]}))
        rows = self.client.get('/api/accounts/banned').get_json()['accounts']
        self.assertEqual(rows[0]['password'], 'saved-password')
        self.assertEqual(rows[0]['banned_at'], '2026-09-17T10:00:00-0300')
        self.assertIsNone(rows[1]['password'])

    def test_confirmation_required_and_unknown_receipt_rejected(self):
        result = self.preflight()
        for body, status in [({**self.body, 'preflight_id': result['preflight_id']}, 400),
                             ({**self.body, 'remove_restricted': True}, 400),
                             ({**self.body, 'preflight_id': 'forged', 'remove_restricted': True}, 409)]:
            self.assertEqual(self.client.post('/api/campaigns', json=body).status_code, status)
        self.runner.start.assert_not_called()
