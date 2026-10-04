"""Reviewed receipt validity at the final local admission, using inert accounts."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from moneymin import recovery
from moneymin.web import server


class CampaignReceiptAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='receipt-admission-')))
        secrets = self.root / 'secrets'
        secrets.mkdir()
        for name, value in [('DATA_DIR', self.root), ('SECRETS_DIR', secrets), ('ROOT', self.root),
                            ('LIBRARY_ROOT', self.root), ('MEDIA_DATA_DIR', self.root / 'media')]:
            self.stack.enter_context(patch.object(server.config, name, value))
        for name, filename in [('PREFS_PATH', 'prefs.json'), ('BALANCES_PATH', 'balances.json'),
                               ('CROWTADO_PW_PATH', 'passwords.json'), ('ACCOUNT_HEALTH_PATH', 'health.json')]:
            self.stack.enter_context(patch.object(server, name, self.root / filename))
        self.email = 'fixture-owner@example.invalid'
        self.token = server.config.token_path(self.email)
        self.token.write_text(json.dumps({'email': self.email, 'localId': 'fixture-subject',
                                          'idToken': 'inert-token', 'refreshToken': 'inert-refresh'}))
        self.stack.enter_context(patch.object(server, '_list_accounts', return_value=[{'email': self.email}]))
        self.stack.enter_context(patch.object(server, '_resolve_org', return_value='fixture-org'))
        self.catalog = self.stack.enter_context(patch.object(server.campaign, 'available_tasks', return_value=[{
            'id': 'fixture-task', 'name': 'Fixture', 'scenario': 'fixture-scenario',
            'clip_count': 1, 'available_for_duration': True}]))
        self.ready = self.stack.enter_context(patch.object(server.readiness, 'campaign_readiness',
                                   return_value={'ready': True, 'checks': []}))
        self.stack.enter_context(patch.object(server, '_storage_snapshot', return_value={'free_bytes': 100 * 1024**3}))
        self.recovery = self.stack.enter_context(patch.object(recovery, 'snapshot', return_value={'items': []}))
        self.stack.enter_context(patch.object(server, 'RECOVERY', Mock(running=False)))
        for name in ['HOLO_CACHE_RUNNER', 'BALANCES_RUNNER', 'ORG_MIGRATION']:
            self.stack.enter_context(patch.object(server, name, Mock(running=False)))
        self.runner = Mock(running=False, state='idle', total_sends=1)
        self.stack.enter_context(patch.object(server, 'RUNNER', self.runner))
        self.ban = self.stack.enter_context(patch.object(server, '_ban_accounts'))
        self.session = self.stack.enter_context(patch.object(server.Session, 'from_email',
                                side_effect=AssertionError('No provider/session access in this fixture')))
        self.now = [1000.0]
        self.stack.enter_context(patch.object(server.time, 'monotonic', side_effect=lambda: self.now[0]))
        self.app = server.create_app(for_testing=True)
        self.client = self.app.test_client()
        self.body = {'accounts': [self.email], 'tasks': [{'task_id': 'fixture-task'}], 'dataset': 'ego4d'}
        preview = self.client.post('/api/campaigns/preflight', json=self.body)
        self.assertEqual(preview.status_code, 200, preview.get_json())
        self.assertTrue(preview.get_json()['ok'])
        self.receipt = preview.get_json()['preflight_id']
        self.start_body = {**self.body, 'preflight_id': self.receipt}

    def assert_blocked(self, response, code):
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(response.get_json()['error_code'], code)
        self.runner.start.assert_not_called()
        self.ban.assert_not_called()
        self.session.assert_not_called()
        self.assertNotIn('inert-token', response.get_data(as_text=True))
        self.assertNotIn('fixture-subject', response.get_data(as_text=True))

    def mutate_owner(self):
        token = json.loads(self.token.read_text())
        token['localId'] = 'another-fixture-subject'
        self.token.write_text(json.dumps(token))

    def test_expiration_during_readiness_blocks_before_start(self):
        def ready(*args, **kwargs):
            self.now[0] = 1600.0
            return {'ready': True, 'checks': []}
        self.ready.side_effect = ready
        self.assert_blocked(self.client.post('/api/campaigns', json=self.start_body), 'preflight_expired')

    def test_owner_change_during_readiness_blocks_before_start(self):
        def ready(*args, **kwargs):
            self.mutate_owner()
            return {'ready': True, 'checks': []}
        self.ready.side_effect = ready
        self.assert_blocked(self.client.post('/api/campaigns', json=self.start_body), 'preflight_accounts_changed')

    def test_expiration_during_final_recovery_read_blocks_before_start(self):
        def snapshot():
            self.now[0] = 1600.0
            return {'items': []}
        self.recovery.side_effect = snapshot
        self.assert_blocked(self.client.post('/api/campaigns', json=self.start_body), 'preflight_expired')

    def test_owner_change_during_final_recovery_read_blocks_before_start(self):
        def snapshot():
            self.mutate_owner()
            return {'items': []}
        self.recovery.side_effect = snapshot
        self.assert_blocked(self.client.post('/api/campaigns', json=self.start_body), 'preflight_accounts_changed')

    def test_two_admitted_requests_cannot_consume_one_receipt_twice(self):
        barrier = threading.Barrier(2)
        # Account-operation hooks serialize starts; race only at HTTP entry.
        responses = []
        def send():
            barrier.wait(timeout=3)
            with self.app.test_client() as client:
                responses.append(client.post('/api/campaigns', json=self.start_body))
        threads = [threading.Thread(target=send) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(responses), 2)
        self.assertEqual(sorted(response.status_code for response in responses), [200, 200])
        # The legacy first-success reply omits already_running; the durable
        # replay explicitly identifies the same previously admitted UUID.
        self.assertEqual(sorted(r.get_json().get('already_running', False) for r in responses), [False, True])
        self.assertTrue(all(r.get_json()['start_request_id'] == self.receipt for r in responses))
        self.runner.start.assert_called_once()
        self.session.assert_not_called()

    def test_valid_receipt_keeps_catalog_and_exact_operation(self):
        before = self.catalog.call_count
        response = self.client.post('/api/campaigns', json=self.start_body)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.runner.start.assert_called_once()
        cfg = self.runner.start.call_args.args[0]
        self.assertEqual(cfg.start_request_id, self.receipt)
        self.assertEqual([a.email for a in cfg.accounts], [self.email])
        self.assertEqual([t.task_id for t in cfg.tasks], ['fixture-task'])
        self.assertEqual(self.catalog.call_count, before)
        self.session.assert_not_called()

    def test_session_rotation_during_readiness_keeps_review_valid(self):
        def ready(*args, **kwargs):
            token = json.loads(self.token.read_text())
            token.update(idToken='inert-rotated-token', refreshToken='inert-rotated-refresh', expires_at=100000.0)
            self.token.write_text(json.dumps(token))
            return {'ready': True, 'checks': []}
        self.ready.side_effect = ready
        response = self.client.post('/api/campaigns', json=self.start_body)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.runner.start.assert_called_once()

    def test_retry_of_already_running_operation_does_not_restart(self):
        def start(cfg):
            self.runner.running = True
            self.runner.state = 'running'
        self.runner.start.side_effect = start
        first = self.client.post('/api/campaigns', json=self.start_body)
        self.assertEqual(first.status_code, 200, first.get_json())
        self.now[0] = 10000.0
        retry = self.client.post('/api/campaigns', json=self.start_body)
        self.assertEqual(retry.status_code, 200)
        self.assertTrue(retry.get_json()['already_running'])
        self.runner.start.assert_called_once()

    def test_failed_runner_start_keeps_unknown_claim_without_automatic_retry(self):
        self.runner.start.side_effect = RuntimeError('inert-thread-error')
        first = self.client.post('/api/campaigns', json=self.start_body)
        self.assertEqual(first.status_code, 500, first.get_json())
        self.assertEqual(first.get_json()['error_code'], 'start_outcome_unknown')
        self.runner.start.side_effect = None
        second = self.client.post('/api/campaigns', json=self.start_body)
        self.assertEqual(second.status_code, 500, second.get_json())
        self.assertEqual(second.get_json()['error_code'], 'start_outcome_unknown')
        self.assertEqual(self.runner.start.call_count, 1)
        status = self.client.get('/api/campaigns/starts/' + self.receipt).get_json()
        self.assertEqual(status['status'], 'review')
        self.assertTrue(status['outcome_unknown'])
        self.assertFalse(status['may_start'])

    def test_unreviewed_legacy_start_still_uses_current_catalog(self):
        before = self.catalog.call_count
        response = self.client.post('/api/campaigns', json=self.body)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.runner.start.assert_called_once()
        self.assertIsNone(self.runner.start.call_args.args[0].start_request_id)
        self.assertEqual(self.catalog.call_count, before + 1)
