"""Supplemental local publication guards, independent of the paired witnesses."""
import copy
import json
import math
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from moneymin import campaign, campaign_types, recovery, upload
from moneymin.web import runner
import test_campaign_current_evidence as fixtures


class CampaignPublicationGuardTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CampaignCurrentEvidenceTests(
            'test_current_confirmed_receipt_keeps_original_failed_attempt_and_bytes')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.email = self.fixture.row['account_email']

    def test_incomplete_or_conflicting_history_cannot_hide_an_acknowledged_receipt(self):
        self.fixture.write_row()
        histories = []
        for field, value in [('email', 'other@example.invalid'), ('org_key', 'other'),
                             ('session_id', 'other'), ('org_key', None), ('ok', 'true')]:
            history = copy.deepcopy(self.fixture.history)
            history['items'][0]['accounts'][0][field] = value
            histories.append((field, history))
        for field in ('task_id', 'clip_uid', 'registry_key'):
            history = copy.deepcopy(self.fixture.history)
            history['items'][0][field] = 'other'
            histories.append((field, history))
        duplicate = copy.deepcopy(self.fixture.history)
        duplicate['items'][0]['accounts'].append(copy.deepcopy(duplicate['items'][0]['accounts'][0]))
        histories.extend([('duplicate SID', duplicate), ('empty history', {'items': []})])
        for name, history in histories:
            with self.subTest(history=name):
                self.fixture.path.write_text(json.dumps(history), encoding='utf-8')
                before = self.fixture.path.read_bytes()
                snapshot = recovery.snapshot()
                self.assertEqual(len(snapshot['items']), 1)
                self.assertIs(snapshot['items'][0]['publication_pending'], True)
                self.assertIs(snapshot['items'][0]['can_resume'], False)
                self.assertEqual(snapshot['reconciliation_pending'], 0)
                self.assertEqual(self.fixture.path.read_bytes(), before)

    def test_unreadable_histories_keep_receipts_visible_without_exporting_raw_data(self):
        self.fixture.write_row()
        for raw in ('{', '[]', '{"items":[],"items":[]}', '{"items":[],"private":NaN}',
                    '{"items":[{"accounts":{}}]}'):
            with self.subTest(raw=raw):
                self.fixture.path.write_text(raw, encoding='utf-8')
                before = self.fixture.path.read_bytes()
                snapshot = recovery.snapshot()
                self.assertEqual(snapshot['publication_pending'], 1)
                self.assertFalse(snapshot['items'][0]['can_resume'])
                self.assertEqual(self.fixture.path.read_bytes(), before)
                self.assertNotIn('PRIVATE_SENTINEL', json.dumps(snapshot))

    def test_acknowledged_orphan_is_never_an_authorization_to_resume_transport(self):
        self.fixture.write_row()
        self.fixture.path.write_text('{"items":[]}', encoding='utf-8')
        with patch.object(upload, 'pump_pending', side_effect=AssertionError('no transport')) as pump, \
             patch.object(campaign.Session, 'from_email', side_effect=AssertionError('no auth')) as auth:
            with self.assertRaises(ValueError):
                recovery.resume_account(self.email, lambda _: 'org')
        pump.assert_not_called()
        auth.assert_not_called()

    def test_worker_separates_history_attention_from_unfinished_delivery(self):
        worker = recovery.RecoveryRunner()
        items = [{'email': self.email, 'status': 'confirmed', 'index_reconciled': True,
                  'publication_pending': True, 'can_resume': False}]
        with patch.object(recovery, 'resume_account', return_value={'items': items, 'reconciled': 1}):
            worker._run(self.email, lambda _: 'org')
        result = worker.snapshot()
        self.assertEqual(result['state'], 'done')
        self.assertEqual(result['result']['publication_pending'], 1)
        for state in ('pending', 'needs_review'):
            with self.subTest(state=state), patch.object(recovery, 'resume_account', return_value={
                    'items': [{**items[0], 'status': state, 'index_reconciled': False}]}):
                worker._run(self.email, lambda _: 'org')
                self.assertEqual(worker.snapshot()['state'], 'pending')

    def log_and_config(self, duration=32_123):
        log = campaign_types.CampaignLog('declared fixture', [self.email])
        log._path = self.fixture.path
        item = copy.deepcopy(self.fixture.history['items'][0])
        item['duration_ms'] = duration
        item['accounts'][0].update(ok=True, finalized=True)
        log.items = [item]
        cfg = campaign_types.CampaignConfig(
            accounts=[campaign_types.AccountSpec(self.email, 'org')],
            tasks=[campaign_types.TaskSpec('task', 'declared fixture', 30, 60)])
        instance = runner.CampaignRunner()
        instance.account_seconds = {self.email: 0.0}
        return log, cfg, instance

    def test_progress_requires_current_receipts_and_credits_duration_only_once(self):
        log, cfg, instance = self.log_and_config()
        instance._restore_published_progress(log, cfg)
        self.assertEqual(instance.ok_sends, 0, 'legacy success alone is not a current receipt')
        self.fixture.write_row()
        for _ in range(2):
            instance._restore_published_progress(log, cfg)
        self.assertEqual((instance.ok_sends, instance.done_sends), (1, 1))
        self.assertAlmostEqual(instance.account_seconds[self.email], 32.123)
        self.assertFalse(instance.events, 'restoration cannot invent a success event')
        self.assertEqual(instance.state, 'idle')

    def test_invalid_durations_do_not_mask_an_error_or_inflate_progress(self):
        self.fixture.write_row()
        for duration in (True, '32000', None, float('inf'), float('nan'), -1, 10**400):
            with self.subTest(duration_type=type(duration).__name__):
                log, cfg, instance = self.log_and_config(duration)
                instance._restore_published_progress(log, cfg)
                self.assertEqual((instance.ok_sends, instance.done_sends), (1, 1))
                self.assertEqual(instance.account_seconds[self.email], 0.0)
                self.assertTrue(math.isfinite(instance.account_seconds[self.email]))

    def test_progress_reader_failure_still_publishes_original_error_state(self):
        log, cfg, instance = self.log_and_config()
        error = RuntimeError('declared original observer error')
        error._moneymin_campaign_log = log
        with patch.object(runner, 'run_campaign', side_effect=error), \
             patch.object(instance, '_restore_published_progress', side_effect=OSError('private-read-error')):
            instance._run(cfg)
        self.assertEqual(instance.state, 'error')
        self.assertEqual(instance.ok_sends, 0)
        self.assertNotIn('private-read-error', json.dumps(instance.snapshot()))

    def test_private_log_reference_cannot_replace_the_original_exception(self):
        class ObserverError(RuntimeError):
            def __setattr__(self, name, value):
                if name.startswith('_moneymin_'):
                    raise AttributeError('declared attribute restriction')
                return super().__setattr__(name, value)
        error = ObserverError('declared original observer error')
        log, cfg, _instance = self.log_and_config()
        with patch.object(campaign, '_run_campaign', side_effect=error), \
             patch.object(campaign, '_ClipPrefetch', return_value=SimpleNamespace(shutdown=lambda: None)):
            with self.assertRaises(ObserverError) as caught:
                campaign.run_campaign(cfg, log=log)
        self.assertIs(caught.exception, error)
