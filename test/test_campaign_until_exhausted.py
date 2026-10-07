"""Open campaigns ignore legacy goals/counts without weakening delivery guards."""
import unittest
from unittest.mock import patch

from moneymin import campaign
from moneymin.web import server
from moneymin.web.runner import CampaignRunner, _public_event
import test_campaign_capacity as capacity_tests
import test_campaign_selection as selection_tests


class CampaignUntilExhaustedTests(unittest.TestCase):
    def api_fixture(self):
        case = capacity_tests.CampaignCapacityTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        fixture = case.fixture()
        fixture.body.pop('run_until_exhausted', None)
        fixture.body['include_clip_plan'] = True
        fixture.candidates = [
            {'clip_uid': f'clip-{i}', 'source': 'ego4d', 'dur_s': 300,
             'parent_video_uid': f'parent-{i}'} for i in range(3)]
        return fixture

    def test_default_preview_and_start_ignore_old_eight_hour_goal_and_count(self):
        case = self.api_fixture()
        body = {**case.body, 'target_hours': 8, 'count': 1}
        review = case.client.post('/api/campaigns/preflight', json=body).get_json()
        self.assertTrue(review['ok'], review)
        self.assertTrue(review['run_until_exhausted'])
        self.assertEqual(review['target_hours'], 0)
        self.assertEqual(review['capacity']['target_seconds_per_account'], 0)
        self.assertEqual(review['capacity']['shortfall_account_count'], 0)
        self.assertEqual(review['estimated_sends'], 3)
        response = case.client.post('/api/campaigns', json={**body, 'preflight_id': review['preflight_id']})
        self.assertEqual(response.status_code, 200, response.get_json())
        cfg = case.runner.start.call_args.args[0]
        self.assertTrue(cfg.run_until_exhausted)
        self.assertEqual(cfg.target_hours_per_account, 0)
        self.assertEqual(len(cfg.candidate_plan['task']), 3)

    def test_direct_start_needs_no_hours(self):
        case = self.api_fixture()
        response = case.client.post('/api/campaigns', json=case.body)
        self.assertEqual(response.status_code, 200, response.get_json())
        cfg = case.runner.start.call_args.args[0]
        self.assertTrue(cfg.run_until_exhausted)
        self.assertEqual(cfg.target_hours_per_account, 0)

    def test_pending_candidates_are_excluded_without_hour_shortfall(self):
        case = self.api_fixture()
        pending = [{'email': 'good@example.com', 'clip_uid': 'clip-0', 'blocks_campaign': False}]
        with patch.object(server.recovery, 'snapshot', return_value={'items': pending}):
            review = case.client.post('/api/campaigns/preflight', json=case.body).get_json()
        self.assertTrue(review['ok'], review)
        self.assertEqual(review['estimated_sends'], 2)
        self.assertEqual(review['capacity']['accounts'][0]['pending_clips'], 1)

    def test_unknown_pending_delivery_still_blocks(self):
        case = self.api_fixture()
        with patch.object(server.recovery, 'snapshot', return_value={'items': [
                {'email': 'good@example.com', 'clip_uid': None, 'blocks_campaign': True}]}):
            review = case.client.post('/api/campaigns/preflight', json=case.body).get_json()
        self.assertFalse(review['ok'])
        self.assertTrue(any('sem clipe identificado' in b for b in review['blockers']))

    def test_mode_must_be_boolean(self):
        case = self.api_fixture()
        for route in ('/api/campaigns/preflight', '/api/campaigns'):
            response = case.client.post(route, json={**case.body, 'run_until_exhausted': 'true'})
            self.assertEqual(response.status_code, 400)
        case.runner.start.assert_not_called()

    def run_engine(self, *, stop_after=None):
        case = selection_tests.CampaignSelectionTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        cfg = case.cfg
        cfg.accounts = cfg.accounts[:2]
        cfg.tasks = cfg.tasks[:1]
        cfg.run_until_exhausted = True
        cfg.target_hours_per_account = 8
        cfg.account_workers = 1
        cfg.account_gap_s = 0
        cfg.candidate_plan = {cfg.tasks[0].task_id: [
            {'clip_uid': f'fresh-{i}', 'source': 'ego4d', 'dur_s': 300,
             'parent_video_uid': f'parent-{i}'} for i in range(4)]}
        calls, events = [], []
        def send(item, account, *args, **kwargs):
            calls.append((item['clip_uid'], account.email))
            return {'email': account.email, 'ok': True, 'finalized': True}
        with patch.object(campaign, '_ego_clip_inputs', return_value=({}, {})), \
             patch.object(campaign, 'prepare_clip', side_effect=lambda *a, **k: {
                 'duration_ms': 300000, 'imu_real': True, 'video_path': str(case.tmp / 'fake.mp4')}), \
             patch.object(campaign, 'upload_to_account', side_effect=send), \
             patch.object(campaign.sent_registry, 'mark_sent'), \
             patch.object(campaign, '_enforce_account_video_cache', return_value=(0, 0)), \
             patch.object(campaign, '_prefetch_following'):
            result = campaign.run_campaign(cfg,
                should_stop=lambda: stop_after is not None and len(calls) >= stop_after,
                progress=lambda k, p: events.append((k, p)))
        return result, calls, events

    def test_engine_consumes_all_four_candidates_for_both_accounts(self):
        result, calls, events = self.run_engine()
        self.assertEqual(len(calls), 8)
        self.assertEqual(len(set(calls)), 8)
        self.assertEqual(result.status, 'done')
        self.assertFalse(any(k in {'goal_shortfall', 'task_shortfall'} for k, _ in events))
        self.assertEqual(events[-1][0], 'campaign_done')
        task_start = next(p for k, p in events if k == 'task_start')
        self.assertTrue(task_start['run_until_exhausted'])
        self.assertIn('até esgotar o conteúdo', _public_event('task_start', task_start)['detail'])
        self.assertNotIn('Selecionando 1', _public_event('task_start', task_start)['detail'])

    def test_user_stop_prevents_next_candidate_and_preserves_stopped_state(self):
        result, calls, events = self.run_engine(stop_after=2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result.status, 'stopped')
        self.assertTrue(any(k == 'campaign_stopped' for k, _ in events))
        self.assertFalse(any(k == 'campaign_done' for k, _ in events))

    def test_runner_has_no_goal_or_one_clip_progress_ceiling(self):
        case = selection_tests.CampaignSelectionTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.cfg.run_until_exhausted = True
        case.cfg.target_hours_per_account = 8
        runner = CampaignRunner()
        with patch('moneymin.web.runner.threading.Thread'):
            runner.start(case.cfg)
        snapshot = runner.snapshot()
        self.assertTrue(snapshot['run_until_exhausted'])
        self.assertEqual(snapshot['totals']['progress_target'], 0)
        self.assertEqual(snapshot['totals']['total_sends'], 0)

