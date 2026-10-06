"""New-footage capacity is checked locally before admitting an hours goal."""
import json
from unittest.mock import patch
import unittest

from moneymin import campaign, campaign_plan, sent_registry, upload
from moneymin.web import server
import test_preflight_continuation as continuation


class CampaignCapacityTests(unittest.TestCase):
    def row(self, uid, window, *, parent='parent', task='task', email='fixture@example.invalid'):
        return {'clip_uid': uid, 'task_id': task, 'source': 'ego4d',
                'parent_video_uid': parent, 'window_s': window,
                'duration_s': window[1] - window[0], 'eligible_accounts': [email]}

    def test_material_overlap_does_not_turn_union_into_executable_capacity(self):
        rows = [self.row('one', [0, 300]), self.row('two', [100, 400], task='other')]
        before = json.dumps(rows, sort_keys=True)
        result = campaign_plan.capacity(rows, ['fixture@example.invalid'], target_seconds=350)
        account = result['accounts'][0]
        self.assertEqual(account['unique_footage_upper_bound_seconds'], 400)
        self.assertEqual(account['available_seconds'], 300)
        self.assertEqual(result['estimated_sends'], 1)
        self.assertFalse(result['can_reach_goal'])
        self.assertEqual(json.dumps(rows, sort_keys=True), before)

    def test_disjoint_cuts_and_small_shared_padding_remain_available(self):
        rows = [self.row('one', [0, 300]), self.row('two', [298, 600]),
                self.row('three', [900, 1200])]
        result = campaign_plan.capacity(rows, ['fixture@example.invalid'], target_seconds=900)
        self.assertEqual(result['accounts'][0]['available_seconds'], 902)
        self.assertEqual(result['accounts'][0]['unique_footage_seconds'], 900)
        self.assertEqual(result['accounts'][0]['estimated_executable_seconds'], 902)
        self.assertEqual(result['estimated_sends'], 3)
        self.assertTrue(result['can_reach_goal'])

    def test_permitted_overlap_counts_the_delivery_durations_toward_the_goal(self):
        rows = [self.row('one', [0, 300]), self.row('two', [150, 450])]
        result = campaign_plan.capacity(rows, ['fixture@example.invalid'], target_seconds=540)
        self.assertEqual(result['accounts'][0]['available_seconds'], 600)
        self.assertEqual(result['accounts'][0]['unique_footage_seconds'], 450)
        self.assertEqual(result['accounts'][0]['unique_footage_upper_bound_seconds'], 450)
        self.assertEqual(result['estimated_sends'], 2)
        self.assertTrue(result['can_reach_goal'])

    def test_capacity_and_estimated_sends_are_per_account_and_respect_count(self):
        rows = [self.row('one', [0, 300]), self.row('two', [300, 600])]
        rows[0].update(excluded_accounts=['used@example.invalid'],
                       recorded_accounts=['used@example.invalid'], pending_accounts=[])
        rows[1].update(eligible_accounts=['fixture@example.invalid', 'used@example.invalid'])
        result = campaign_plan.capacity(rows, ['fixture@example.invalid', 'used@example.invalid'],
                                        count_per_task=1)
        self.assertEqual(result['estimated_sends'], 2)
        self.assertEqual(result['accounts'][1]['recorded_clips'], 1)
        self.assertEqual(result['available_seconds_min'], 300)
        self.assertEqual(result['available_seconds_max'], 300)

    def test_source_identity_keeps_distinct_providers_separate(self):
        rows = [self.row('one', [0, 300]), self.row('two', [0, 300])]
        rows[1]['source'] = 'holoassist'
        result = campaign_plan.capacity(rows, ['fixture@example.invalid'], target_seconds=600)
        self.assertEqual(result['accounts'][0]['available_seconds'], 600)
        self.assertEqual(result['estimated_sends'], 2)
        self.assertTrue(result['can_reach_goal'])

    def fixture(self):
        case = continuation.PreflightContinuationTests('test_remove_continue_archives_password_and_date_without_tokens')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.body['accounts'] = ['good@example.com']
        case.stack.enter_context(patch.object(server.recovery, 'snapshot', return_value={'items': []}))
        case.candidates = [{'clip_uid': 'clip', 'source': 'ego4d', 'dur_s': 300}]
        case.automatic = case.stack.enter_context(patch.object(
            campaign, 'automatic_candidates', side_effect=lambda task, config:
            config.candidate_plan[task.task_id] if config.candidate_plan is not None else case.candidates))
        return case

    def test_positive_goal_requires_capacity_even_without_optional_clip_plan(self):
        case = self.fixture()
        for optional in (None, False):
            body = {**case.body, 'target_hours': 8}
            if optional is not None:
                body['include_clip_plan'] = optional
            result = case.client.post('/api/campaigns/preflight', json=body).get_json()
            self.assertFalse(result['ok'])
            self.assertIsNone(result['preflight_id'])
            self.assertTrue(result['capacity']['known'])
            self.assertEqual(result['capacity']['target_seconds_per_account'], 28800)
            self.assertEqual(result['capacity']['available_seconds_max'], 300)
            self.assertEqual(result['estimated_sends'], 1)
            self.assertEqual(result['capacity']['shortfall_account_count'], 1)
        case.runner.start.assert_not_called()

    def test_legacy_start_rejects_without_claim_journal_or_seeded_registry_write(self):
        case = self.fixture()
        history = case.root / 'campaign_old.json'
        history.write_text(json.dumps({'items': [{
            'clip_uid': 'clip', 'registry_key': 'minute|task|Task',
            'accounts': [{'email': 'good@example.com', 'ok': True, 'finalized': True}]}]}), encoding='utf8')
        before = {path: path.read_bytes() for path in case.root.rglob('*') if path.is_file()}
        with patch.object(sent_registry, '_save', side_effect=AssertionError('registry write')), \
             patch.object(server.campaign_start_store, 'claim', side_effect=AssertionError('start claim')), \
             patch.object(upload, 'save_sidecar', side_effect=AssertionError('journal write')):
            review = case.client.post('/api/campaigns/preflight', json={**case.body, 'target_hours': 8}).get_json()
            response = case.client.post('/api/campaigns', json={**case.body, 'target_hours': 8})
        self.assertFalse(review['ok'])
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(response.get_json()['error_code'], 'campaign_capacity_insufficient')
        self.assertEqual(response.get_json()['capacity']['accounts'][0]['recorded_clips'], 1)
        self.assertFalse((case.root / sent_registry.FILE_NAME).exists())
        # The pre-existing integrity lookup takes a lease, whose empty lock
        # file is not a start reservation or a persisted delivery.
        after = {path: path.read_bytes() for path in case.root.rglob('*')
                 if path.is_file() and path.name != 'start_requests.lock'}
        self.assertEqual(before, after)
        case.runner.start.assert_not_called()

    def test_reviewed_start_rechecks_new_pending_reservation_before_claim(self):
        case = self.fixture()
        body = {**case.body, 'target_hours': .05}
        review = case.client.post('/api/campaigns/preflight', json=body).get_json()
        self.assertTrue(review['ok'], review)
        self.assertEqual(review['estimated_sends'], 1)
        pending = [{'email': 'good@example.com', 'clip_uid': 'clip', 'blocks_campaign': False}]
        with patch.object(server.recovery, 'snapshot', return_value={'items': pending}), \
             patch.object(server.campaign_start_store, 'claim', side_effect=AssertionError('start claim')):
            response = case.client.post('/api/campaigns', json={**body, 'preflight_id': review['preflight_id']})
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(response.get_json()['error_code'], 'campaign_capacity_insufficient')
        self.assertEqual(response.get_json()['capacity']['accounts'][0]['pending_clips'], 1)
        case.runner.start.assert_not_called()

    def test_legacy_sufficient_goal_freezes_the_checked_pool(self):
        case = self.fixture()
        response = case.client.post('/api/campaigns', json={**case.body, 'target_hours': .05})
        self.assertEqual(response.status_code, 200, response.get_json())
        config = case.runner.start.call_args.args[0]
        self.assertEqual(config.candidate_plan, {'task': case.candidates})
        self.assertEqual(config.target_hours_per_account, .05)

    def test_reviewed_start_keeps_approved_candidates_when_catalog_changes(self):
        case = self.fixture()
        body = {**case.body, 'target_hours': .05}
        review = case.client.post('/api/campaigns/preflight', json=body).get_json()
        self.assertTrue(review['ok'], review)
        approved = []
        def candidates(task, config):
            approved.append(config.candidate_plan)
            return config.candidate_plan[task.task_id] if config.candidate_plan is not None else case.candidates
        case.automatic.side_effect = candidates
        case.candidates = [{'clip_uid': 'unreviewed', 'source': 'ego4d', 'dur_s': 900}]
        response = case.client.post('/api/campaigns', json={**body, 'preflight_id': review['preflight_id']})
        self.assertEqual(response.status_code, 200, response.get_json())
        config = case.runner.start.call_args.args[0]
        self.assertIs(config.candidate_plan, approved[-1])
        self.assertEqual(config.candidate_plan['task'][0]['clip_uid'], 'clip')

    def test_zero_goal_keeps_explicit_count_without_capacity_requirement(self):
        case = self.fixture()
        case.automatic.side_effect = AssertionError('selection not requested')
        body = {**case.body, 'target_hours': 0, 'count': 3}
        review = case.client.post('/api/campaigns/preflight', json=body).get_json()
        self.assertTrue(review['ok'], review)
        self.assertIsNone(review['capacity'])
        response = case.client.post('/api/campaigns', json=body)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(case.runner.start.call_args.args[0].tasks[0].count, 3)
        case.automatic.assert_not_called()

    def test_unavailable_capacity_cannot_start_positive_goal(self):
        case = self.fixture()
        case.automatic.side_effect = ValueError('unavailable fixture')
        response = case.client.post('/api/campaigns', json={**case.body, 'target_hours': 8})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['error_code'], 'campaign_capacity_unavailable')
        self.assertIsNone(response.get_json()['capacity'])
        case.runner.start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
