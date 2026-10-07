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
        for name, value in [('DATA_DIR', self.root), ('SECRETS_DIR', self.secrets), ('ROOT', self.root),
                            ('LIBRARY_ROOT', self.root), ('MEDIA_DATA_DIR', self.root / 'media')]:
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
        self.body = {'run_until_exhausted': False, 'accounts': ['good@example.com', 'bad@example.com'],
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
        self.client = server.create_app(for_testing=True).test_client()

    def preflight(self):
        response = self.client.post('/api/campaigns/preflight', json=self.body)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def test_remove_continue_archives_password_and_date_without_tokens(self):
        result = self.preflight()
        self.assertTrue(result['ok'])
        self.assertFalse(result['can_remove_and_continue'])
        self.assertEqual(result['removed_accounts'], ['bad@example.com'])
        self.assertEqual(result['account_issues'], [])
        self.assertNotIn('bad@example.com', ' '.join(result['blockers']))
        self.assertIn('Banidas', ' '.join(result['warnings']))
        self.assertFalse(server.config.token_path('bad@example.com').exists())
        before = (self.resolve.call_count, self.catalog.call_count, self.ready.call_count)
        response = self.client.post('/api/campaigns', json={**self.body, 'preflight_id': result['preflight_id']})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(before[:2], (self.resolve.call_count, self.catalog.call_count))
        self.assertEqual(self.ready.call_count, before[2] + 1)
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
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.get_json()['already_running'])
        self.runner.start.assert_called_once()
        self.assertEqual(self.client.get('/api/accounts/banned').get_json()['accounts'][0]['banned_at'], saved_date)

    def test_temporary_failure_never_offers_permanent_removal(self):
        self.failure = TimeoutError('timeout')
        result = self.preflight()
        self.assertFalse(result['can_remove_and_continue'])
        self.assertTrue(result['can_skip_and_continue'])
        self.assertTrue(result['preflight_id'])
        self.assertEqual(result['removed_accounts'], [])
        self.assertTrue(server.config.token_path('bad@example.com').exists())
        self.assertFalse((self.root / 'banned_accounts.json').exists())

    def test_permission_failure_continues_once_without_removing_or_revalidating(self):
        self.failure = PermissionError('private-path-must-not-appear')
        result = self.preflight()
        self.assertFalse(result['ok'])
        self.assertTrue(result['can_skip_and_continue'])
        self.assertEqual(result['skippable_accounts'], ['bad@example.com'])
        self.assertEqual(result['accounts']['validated'], 1)
        self.assertEqual(result['account_issues'][0]['code'], 'local_permission')
        self.assertNotIn('private-path', json.dumps(result))
        request = {**self.body, 'preflight_id': result['preflight_id']}
        self.assertEqual(self.client.post('/api/campaigns', json=request).status_code, 400)
        self.runner.start.assert_not_called()
        before = (self.resolve.call_count, self.catalog.call_count)
        request['skip_unverified'] = True
        for _ in range(2):
            response = self.client.post('/api/campaigns', json=request)
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(response.get_json()['skipped_accounts'], ['bad@example.com'])
            self.assertEqual(response.get_json()['removed_accounts'], [])
        self.assertEqual(before, (self.resolve.call_count, self.catalog.call_count))
        self.runner.start.assert_called_once()
        self.assertEqual([a.email for a in self.runner.start.call_args.args[0].accounts], ['good@example.com'])
        self.assertTrue(server.config.token_path('bad@example.com').exists())
        self.assertNotIn('bad@example.com', server._removed_accounts())
        self.assertIn('bad@example.com', json.loads(server.CROWTADO_PW_PATH.read_text()))

    def test_skip_permission_does_not_override_readiness_or_missing_survivors(self):
        self.failure = PermissionError()
        self.ready.return_value = {'ready': False, 'checks': [{'status': 'error', 'name': 'Disk', 'detail': 'blocked'}]}
        result = self.preflight()
        self.assertFalse(result['can_skip_and_continue'])
        self.assertIsNone(result['preflight_id'])
        self.ready.return_value = {'ready': True, 'checks': []}
        self.body['accounts'] = ['bad@example.com']
        result = self.preflight()
        self.assertFalse(result['can_skip_and_continue'])
        self.assertIsNone(result['preflight_id'])

    def test_skip_permission_in_category_check_excludes_account_from_capacity(self):
        self.failure = PermissionError()
        sessions = self.category_sessions()
        result = self.preflight()
        self.assertTrue(result['can_skip_and_continue'])
        self.assertEqual(result['accounts']['validated'], 1)
        before = (self.resolve.call_count, self.catalog.call_count, sessions.call_count)
        response = self.client.post('/api/campaigns', json={**self.body, 'preflight_id': result['preflight_id'], 'skip_unverified': True})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(before, (self.resolve.call_count, self.catalog.call_count, sessions.call_count))
        self.assertEqual([a.email for a in self.runner.start.call_args.args[0].accounts], ['good@example.com'])
        self.assertTrue(server.config.token_path('bad@example.com').exists())

    def test_skipped_account_cannot_be_substituted_in_reviewed_request(self):
        self.failure = PermissionError()
        result = self.preflight()
        response = self.client.post('/api/campaigns', json={**self.body,
            'accounts': ['bad@example.com'], 'preflight_id': result['preflight_id'], 'skip_unverified': True})
        self.assertEqual(response.status_code, 409)
        self.runner.start.assert_not_called()

    def test_transient_first_catalog_can_continue_with_successfully_checked_peer(self):
        self.failure = TimeoutError('timeout')
        self.category_sessions()
        self.body['accounts'] = ['bad@example.com', 'good@example.com']
        good_catalog = self.catalog.return_value
        self.catalog.side_effect = lambda email, *args, **kwargs: good_catalog if email == 'good@example.com' else (_ for _ in ()).throw(self.failure)
        result = self.preflight()
        self.assertTrue(result['can_skip_and_continue'])
        self.assertEqual(result['skippable_accounts'], ['bad@example.com'])
        self.assertEqual(result['accounts']['validated'], 1)
        response = self.client.post('/api/campaigns', json={**self.body,
            'preflight_id': result['preflight_id'], 'skip_unverified': True})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(server.config.token_path('bad@example.com').exists())

    def test_pending_account_capacity_uses_only_approved_participants(self):
        self.failure = PermissionError()
        self.body['target_hours'] = 8
        candidates = [{'clip_uid': f'fixture-{i}', 'source': 'ego4d', 'dur_s': 900,
                       'parent_video_uid': f'fixture-parent-{i}', 'window_s': [0, 900]}
                      for i in range(32)]
        def pool(task, cfg, **options):
            return cfg.candidate_plan[task.task_id] if cfg.candidate_plan is not None else candidates
        with patch.object(server.campaign, 'automatic_candidates', side_effect=pool), \
             patch.object(server.recovery, 'snapshot', return_value={'items': []}):
            result = self.preflight()
            self.assertTrue(result['can_skip_and_continue'])
            self.assertEqual([row['email'] for row in result['capacity']['accounts']], ['good@example.com'])
            self.assertTrue(result['capacity']['can_reach_goal'])
            before = (self.resolve.call_count, self.catalog.call_count)
            response = self.client.post('/api/campaigns', json={**self.body,
                'preflight_id': result['preflight_id'], 'skip_unverified': True})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(before, (self.resolve.call_count, self.catalog.call_count))
        self.assertEqual(self.runner.start.call_args.args[0].target_hours_per_account, 8)
        self.assertEqual([a.email for a in self.runner.start.call_args.args[0].accounts], ['good@example.com'])
        self.assertTrue(server.config.token_path('bad@example.com').exists())

    def category_sessions(self):
        self.resolve.side_effect = None
        self.resolve.return_value = server.config.ORG_KEY
        def session(email):
            value = Mock()
            value.all_tasks.side_effect = (
                self.failure if email == 'bad@example.com' else lambda _org: [{'id': 'task'}])
            return value
        return self.stack.enter_context(patch.object(server.Session, 'from_email', side_effect=session))

    def test_restriction_during_other_account_categories_starts_survivors_without_revalidation(self):
        sessions = self.category_sessions()
        result = self.preflight()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['removed_accounts'], ['bad@example.com'])
        self.assertEqual(result['accounts']['validated'], 1)
        before = (self.resolve.call_count, self.catalog.call_count, sessions.call_count)
        response = self.client.post('/api/campaigns', json={**self.body, 'preflight_id': result['preflight_id']})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(before, (self.resolve.call_count, self.catalog.call_count, sessions.call_count))
        self.assertEqual([a.email for a in self.runner.start.call_args.args[0].accounts], ['good@example.com'])

    def test_restriction_on_first_catalog_uses_next_account_in_the_same_preflight(self):
        sessions = self.category_sessions()
        self.body['accounts'] = ['bad@example.com', 'good@example.com']
        good_catalog = self.catalog.return_value
        def catalog(email, *_args, **_kwargs):
            if email == 'bad@example.com':
                raise self.failure
            return good_catalog
        self.catalog.side_effect = catalog
        result = self.preflight()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['removed_accounts'], ['bad@example.com'])
        self.assertEqual(result['accounts']['validated'], 1)
        self.assertEqual([call.args[0] for call in self.catalog.call_args_list], ['bad@example.com', 'good@example.com'])
        before = (self.resolve.call_count, self.catalog.call_count, sessions.call_count)
        response = self.client.post('/api/campaigns', json={**self.body, 'preflight_id': result['preflight_id']})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(before, (self.resolve.call_count, self.catalog.call_count, sessions.call_count))
        self.assertEqual([a.email for a in self.runner.start.call_args.args[0].accounts], ['good@example.com'])

    def test_pending_on_healthy_survivor_still_blocks_start(self):
        with patch.object(server.recovery, 'snapshot', return_value={'items': [
                {'email': 'good@example.com', 'blocks_campaign': True, 'clip_uid': None}]}):
            result = self.preflight()
        self.assertFalse(result['ok'])
        self.assertIsNone(result['preflight_id'])
        self.assertIn('good@example.com', ' '.join(result['blockers']))
        self.runner.start.assert_not_called()

    def test_eight_hour_goal_uses_only_survivor_capacity_and_reuses_candidate_plan(self):
        sessions = self.category_sessions()
        self.body['target_hours'] = 8
        candidates = [{'clip_uid': f'fixture-{i}', 'source': 'ego4d', 'dur_s': 900,
                       'parent_video_uid': f'fixture-parent-{i}', 'window_s': [0, 900]}
                      for i in range(32)]
        def pool(task, cfg, **options):
            return cfg.candidate_plan[task.task_id] if cfg.candidate_plan is not None else candidates
        with patch.object(server.campaign, 'automatic_candidates', side_effect=pool), \
             patch.object(server.recovery, 'snapshot', return_value={'items': []}):
            result = self.preflight()
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['accounts']['validated'], 1)
            capacity = result['capacity']
            self.assertTrue(capacity['can_reach_goal'])
            self.assertEqual(capacity['available_seconds_min'], 28800)
            self.assertEqual([row['email'] for row in capacity['accounts']], ['good@example.com'])
            before = (self.resolve.call_count, self.catalog.call_count, sessions.call_count)
            response = self.client.post('/api/campaigns', json={**self.body, 'preflight_id': result['preflight_id']})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(before, (self.resolve.call_count, self.catalog.call_count, sessions.call_count))
        cfg = self.runner.start.call_args.args[0]
        self.assertEqual([a.email for a in cfg.accounts], ['good@example.com'])
        self.assertEqual(cfg.target_hours_per_account, 8)
        self.assertEqual(cfg.candidate_plan['task'], candidates)

    def test_temporary_catalog_failure_without_successful_peer_never_starts(self):
        self.category_sessions()
        self.body['accounts'] = ['bad@example.com', 'good@example.com']
        self.catalog.side_effect = TimeoutError('temporary fixture timeout')
        result = self.preflight()
        self.assertFalse(result['ok'])
        self.assertIsNone(result['preflight_id'])
        self.assertEqual(result['removed_accounts'], [])
        self.assertEqual(self.catalog.call_count, 2)
        self.assertTrue(server.config.token_path('bad@example.com').exists())
        self.runner.start.assert_not_called()

    def test_pending_on_removed_account_does_not_block_healthy_survivor(self):
        with patch.object(server.recovery, 'snapshot', return_value={'items': [
                {'email': 'bad@example.com', 'blocks_campaign': True, 'clip_uid': None}]}):
            result = self.preflight()
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['preflight_id'])
        self.assertEqual(result['removed_accounts'], ['bad@example.com'])

    def test_recovery_read_failure_keeps_preflight_blocked_and_returns_diagnostic(self):
        from moneymin import recovery
        from moneymin.recovery_errors import RecoveryReadError
        self.body['accounts'] = ['good@example.com']
        with patch.object(recovery, 'snapshot', side_effect=RecoveryReadError('reset_history')):
            result = self.preflight()
        self.assertFalse(result['ok'])
        self.assertIsNone(result['preflight_id'])
        self.assertFalse(result['can_remove_and_continue'])
        self.assertEqual(result['recovery_error']['code'], 'reset_history')
        self.assertIn('sent_reset_history.json', ' '.join(result['blockers']))
        self.runner.start.assert_not_called()
        self.assertTrue(server.config.token_path('good@example.com').exists())

    def test_modified_request_or_changed_token_rejects_without_removal(self):
        result = self.preflight()
        receipt = result['preflight_id']
        self.assertTrue(receipt)
        self.assertFalse(server.config.token_path('bad@example.com').exists())
        changed = {**self.body, 'preflight_id': receipt, 'count': 99}
        self.assertEqual(self.client.post('/api/campaigns', json=changed).status_code, 409)
        with patch.object(server.time, 'monotonic', return_value=10**15):
            expired = self.client.post('/api/campaigns', json={**self.body, 'preflight_id': receipt})
        self.assertEqual(expired.status_code, 409)
        self.assertEqual(expired.get_json()['error_code'], 'preflight_expired')
        server.config.token_path('good@example.com').write_text(json.dumps({'email': 'good@example.com', 'idToken': 'changed'}))
        changed_token = self.client.post('/api/campaigns', json={**self.body, 'preflight_id': receipt})
        self.assertEqual(changed_token.status_code, 409)
        self.assertTrue(server.config.token_path('good@example.com').exists())
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

    def test_metadata_and_unrelated_removal_do_not_expire_review(self):
        result = self.preflight()
        path = server.config.token_path('good@example.com')
        token = json.loads(path.read_text())
        token.update(displayName='Updated label', registered=True, kind='login-response')
        path.write_text(json.dumps(token))
        server._set_account_removed('unrelated@example.com', True)
        response = self.client.post('/api/campaigns', json={
            **self.body, 'preflight_id': result['preflight_id'], 'remove_restricted': True})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.runner.start.assert_called_once()

    def test_selected_account_removal_invalidates_review(self):
        result = self.preflight()
        server._set_account_removed('good@example.com', True)
        response = self.client.post('/api/campaigns', json={
            **self.body, 'preflight_id': result['preflight_id'], 'remove_restricted': True})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['error_code'], 'preflight_accounts_changed')
        self.runner.start.assert_not_called()

    def test_expiration_has_specific_reason_and_never_starts(self):
        result = self.preflight()
        with patch.object(server.time, 'monotonic', return_value=10**15):
            response = self.client.post('/api/campaigns', json={
                **self.body, 'preflight_id': result['preflight_id'], 'remove_restricted': True})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['error_code'], 'preflight_expired')
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
        (self.root / 'banned_accounts.json').write_text('[]')
        response = self.client.post('/api/campaigns/preflight', json=self.body)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['code'], 'archived_accounts_unreadable')
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
        forged = self.client.post('/api/campaigns', json={**self.body, 'preflight_id': 'forged', 'remove_restricted': True})
        self.assertEqual(forged.status_code, 409)
        missing = self.client.post('/api/campaigns', json={**self.body, 'remove_restricted': True})
        self.assertEqual(missing.status_code, 400)
        self.runner.start.assert_not_called()
        started = self.client.post('/api/campaigns', json={**self.body, 'preflight_id': result['preflight_id']})
        self.assertEqual(started.status_code, 200, started.get_json())
        self.assertEqual([account.email for account in self.runner.start.call_args.args[0].accounts], ['good@example.com'])
