"""Close barrier and worker/commit lifetimes, without accounts or provider IO."""
from contextlib import ExitStack
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from moneymin.campaign_types import CampaignConfig
from moneymin.web import runner, server
import test_campaign_end_to_end as fixtures


class CampaignShutdownTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.instance = runner.CampaignRunner()
        self.recovery = SimpleNamespace(running=False, _thread=None)
        self.stack.enter_context(patch.object(server, 'RUNNER', self.instance))
        self.stack.enter_context(patch.object(server, 'RECOVERY', self.recovery))
        self.client = server.create_app(for_testing=True).test_client()

    def test_drain_releases_pause_but_waits_for_engine_commits_and_return(self):
        entered, checkpoint, committing, finish = (threading.Event() for _ in range(4))
        def engine(cfg, *, progress, should_stop):
            entered.set()
            if not checkpoint.wait(3):
                raise AssertionError('fixture checkpoint timeout')
            self.assertTrue(should_stop())
            committing.set()
            if not finish.wait(3):
                raise AssertionError('fixture commit timeout')
            progress('campaign_stopped', {})
        with patch.object(runner, 'run_campaign', side_effect=engine):
            self.instance.start(CampaignConfig([], []))
            try:
                self.assertTrue(entered.wait(2))
                self.instance.pause()
                checkpoint.set()
                response = self.client.post('/api/campaigns/drain')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json(), {'ok': True, 'draining': True, 'ready': False})
                self.assertTrue(committing.wait(2))
                self.assertFalse(self.instance.pause_requested)
                self.assertTrue(self.instance.running)
                # A terminal display state alone must never release an alive worker.
                self.instance.state = 'stopped'
                self.assertIs(self.client.post('/api/campaigns/drain').get_json()['ready'], False)
            finally:
                finish.set()
                self.instance._thread.join(3)
        self.assertFalse(self.instance._thread.is_alive())
        self.assertIs(self.client.post('/api/campaigns/drain').get_json()['ready'], True)

    def test_recovery_terminal_label_does_not_hide_an_alive_worker(self):
        release = threading.Event()
        self.recovery._thread = threading.Thread(target=lambda: release.wait(3))
        self.recovery._thread.start()
        try:
            self.assertIs(self.client.post('/api/campaigns/drain').get_json()['ready'], False)
        finally:
            release.set()
            self.recovery._thread.join(3)
        self.assertIs(self.client.post('/api/campaigns/drain').get_json()['ready'], True)

    def test_new_mutations_are_denied_after_close_including_resume(self):
        self.assertIs(self.client.post('/api/campaigns/drain').get_json()['ready'], True)
        with patch.object(self.instance, 'start') as start, patch.object(server.Session, 'from_email') as auth:
            for path in ('/api/campaigns', '/api/campaigns/preflight', '/api/campaigns/resume',
                         '/api/campaigns/pause', '/api/recovery/resume', '/api/recovery/reconcile'):
                with self.subTest(path=path):
                    response = self.client.post(path, json={})
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.get_json()['error_code'], 'campaign_closing')
            start.assert_not_called()
            auth.assert_not_called()
        self.assertEqual(self.client.get('/api/campaigns/current').status_code, 200)

    def test_unauthenticated_close_cannot_set_the_barrier(self):
        with patch.dict('os.environ', {'QMONEY_LOCAL_API_TOKEN': 'inert-local-test-token'}):
            client = server.create_app(for_testing=True).test_client()
        self.assertEqual(client.post('/api/campaigns/drain').status_code, 401)
        with patch.object(self.instance, 'stop') as stop:
            response = client.post('/api/campaigns/drain', headers={'X-QMoney-Session': 'inert-local-test-token'})
            self.assertEqual(response.status_code, 200)
            stop.assert_called_once()

    def test_failed_admitted_request_releases_the_close_barrier(self):
        app = server.create_app(for_testing=True)
        @app.before_request
        def explode():
            if server.request.path == '/api/campaigns':
                raise RuntimeError('inert fixture failure')
        client = app.test_client()
        self.assertEqual(client.post('/api/campaigns', json={}).status_code, 500)
        self.assertIs(client.post('/api/campaigns/drain').get_json()['ready'], True)

    def test_start_already_in_preflight_cannot_win_after_close(self):
        fixture = fixtures.CampaignEndToEndTests('test_success_matches_polling_and_persisted_history')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        entered, release = threading.Event(), threading.Event()
        catalog = [{'id': 'task', 'name': 'Furniture Assembly', 'scenario': 'assembling furniture', 'clip_count': 1}]
        def slow_catalog(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError('fixture preflight timeout')
            return catalog
        result = []
        with patch.object(server.campaign, 'available_tasks', side_effect=slow_catalog), \
             patch.object(fixture.instance, 'start') as start:
            thread = threading.Thread(target=lambda: result.append(
                fixture.client.post('/api/campaigns', json=fixture.body)))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                close_client = fixture.client.application.test_client()
                self.assertIs(close_client.post('/api/campaigns/drain').get_json()['ready'], False)
                start.assert_not_called()
            finally:
                release.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result[0].status_code, 409)
            self.assertEqual(result[0].get_json()['error_code'], 'campaign_closing')
            start.assert_not_called()
            self.assertIs(close_client.post('/api/campaigns/drain').get_json()['ready'], True)
