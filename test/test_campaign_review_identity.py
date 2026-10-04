"""Stable category identity and local readiness/cache changes during review."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from moneymin import campaign, campaign_plan, config, sent_registry
from moneymin.campaign_types import AccountSpec, CampaignConfig, TaskSpec
from moneymin.web import server
import test_preflight_continuation as continuation


class CampaignReviewIdentityTests(unittest.TestCase):
    def test_category_label_changes_union_old_receipts_without_rewriting_index(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(config, 'DATA_DIR', Path(directory)):
            path = Path(directory) / sent_registry.FILE_NAME
            path.write_text(json.dumps({
                'minute|task|Old label': {'clip': ['a@example.invalid']},
                'minute|task|Another | label': {'clip': ['b@example.invalid']},
                'minute|other-task|New label': {'clip': ['unrelated@example.invalid']},
                'scenario': {'clip': ['legacy@example.invalid']},
            }), encoding='utf-8')
            before = path.read_bytes()
            self.assertEqual(sent_registry.sent_emails('minute|task|New label', 'clip'),
                             {'a@example.invalid', 'b@example.invalid'})
            self.assertTrue(sent_registry.is_sent_to_all('minute|task|New label', 'clip',
                            ['a@example.invalid', 'b@example.invalid']))
            self.assertFalse(sent_registry.is_sent_to_all('minute|task|New label', 'clip', ['unrelated@example.invalid']))
            self.assertEqual(sent_registry.sent_emails('scenario', 'clip'), {'legacy@example.invalid'})
            self.assertEqual(path.read_bytes(), before)

    def test_review_and_engine_share_stable_task_identity_including_refined_aliases(self):
        emails = ['a@example.invalid', 'b@example.invalid', 'c@example.invalid']
        task = TaskSpec('task', 'scenario', 60, 600, task_name='Task', task_label='New label')
        cfg = CampaignConfig([AccountSpec(email, 'org') for email in emails], [task])
        candidates = [{'clip_uid': 'refined', 'dedup_clip_uids': ['original'], 'dur_s': 300}]
        registry = {'minute|task|Old label': {'original': emails[:2]}}
        with patch.object(sent_registry, 'load', return_value=registry), \
             patch.object(campaign, 'automatic_candidates', return_value=candidates):
            pools, review, fingerprint = campaign_plan.build(cfg)
            self.assertEqual(review[0]['eligible_accounts'], emails[2:])
            self.assertEqual(review[0]['excluded_accounts'], emails[:2])
            self.assertEqual(campaign._candidate_sent_emails(task.registry_key, candidates[0]), set(emails[:2]))
            self.assertEqual(pools['task'], candidates)
            self.assertEqual(fingerprint, campaign_plan.registry_fingerprint())

    def fixture(self):
        case = continuation.PreflightContinuationTests('test_remove_continue_archives_password_and_date_without_tokens')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.body['accounts'] = ['good@example.com']
        return case

    def test_receipt_rechecks_readiness_without_replacing_the_reviewed_catalog(self):
        case = self.fixture()
        reviewed = case.preflight()
        self.assertIsNotNone(reviewed['preflight_id'])
        body = {**case.body, 'preflight_id': reviewed['preflight_id']}
        calls = case.catalog.call_count
        case.ready.return_value = {'ready': False, 'checks': [{'name': 'fixture', 'status': 'error', 'detail': 'unavailable'}]}
        response = case.client.post('/api/campaigns', json=body)
        self.assertEqual(response.status_code, 400)
        case.runner.start.assert_not_called()
        self.assertEqual(case.catalog.call_count, calls)
        case.ready.return_value = {'ready': True, 'checks': []}
        response = case.client.post('/api/campaigns', json=body)
        self.assertEqual(response.status_code, 200, response.get_json())
        case.runner.start.assert_called_once()
        self.assertEqual(case.catalog.call_count, calls)

    def test_malformed_readiness_cannot_authorize_a_reviewed_start(self):
        case = self.fixture()
        reviewed = case.preflight()
        for readiness in ({}, {'ready': 'true', 'checks': []}, {'ready': None, 'checks': []}):
            with self.subTest(readiness=readiness):
                case.ready.return_value = readiness
                response = case.client.post('/api/campaigns', json={**case.body, 'preflight_id': reviewed['preflight_id']})
                self.assertEqual(response.status_code, 400)
                case.runner.start.assert_not_called()

    def test_async_catalog_changes_owner_but_token_rotation_reuses_the_cache(self):
        case = self.fixture()
        email = 'good@example.com'
        path = server.config.token_path(email)
        token = json.loads(path.read_text())
        token['localId'] = 'fixture-owner-old'
        path.write_text(json.dumps(token), encoding='utf-8')
        case.resolve.side_effect = None
        case.resolve.return_value = 'org-old'
        session = Mock()
        session.all_tasks.return_value = [{'id': 'task', 'name': 'Task'}]
        url = '/api/tasks?async=1&email=' + email + '&dataset=ego4d'
        def read():
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                response = case.client.get(url)
                if response.status_code != 202:
                    return response
                time.sleep(.005)
            raise AssertionError('fixture catalog timeout')
        with patch.object(server.Session, 'from_email', return_value=session):
            first = read()
            self.assertEqual(first.status_code, 200, first.get_json())
            self.assertEqual(first.get_json()['org_key'], 'org-old')
            calls = case.catalog.call_count
            token['idToken'] = 'rotated-fixture-token'
            token['refreshToken'] = 'rotated-fixture-refresh'
            path.write_text(json.dumps(token), encoding='utf-8')
            self.assertEqual(read().get_json()['org_key'], 'org-old')
            self.assertEqual(case.catalog.call_count, calls)
            token['localId'] = 'fixture-owner-new'
            path.write_text(json.dumps(token), encoding='utf-8')
            case.resolve.return_value = 'org-new'
            changed = read()
            self.assertEqual(changed.status_code, 200, changed.get_json())
            self.assertEqual(changed.get_json()['org_key'], 'org-new')
            self.assertEqual(case.catalog.call_count, calls + 1)
