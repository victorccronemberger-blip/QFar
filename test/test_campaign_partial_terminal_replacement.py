"""Resume the remaining recipient after two confirmed deliveries of one source."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from moneymin import campaign, config, recovery, sent_registry, upload
from moneymin.campaign_types import AccountSpec, CampaignConfig, TaskSpec
from moneymin.upload_types import journal_delivery_confirmed
import test_recovery_terminal_failed_protection as fixtures


class PartialTerminalReplacementTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TerminalFailedProtectionTests(
            'test_real_snapshot_keeps_terminal_diagnostics_without_pending_or_sent_credit')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.accounts = [AccountSpec(email, 'fixture-org') for email in (
            'ana@example.invalid', 'ap@example.invalid', 'karina@example.invalid')]
        self.old = {}
        for account in self.accounts:
            name = account.email.split('@')[0]
            row = self.fixture.row(f'old-{name}')
            row['account_email'] = row['remote_terminal_failure']['email'] = account.email
            row['remote_terminal_failure']['user_resource_key'] = f'fixture-user-{name}'
            self.fixture.persist([row])
            self.old[account.email] = row
        self.previous = []
        for account in self.accounts[:2]:
            name = account.email.split('@')[0]
            row = self.fixture.row(f'confirmed-{name}', terminal=False)
            row.update(account_email=account.email, state='done', phase='done',
                finalized=True, campaign_reconciled=True, evaluation_required=False)
            row['campaign_context'].update(retry_of_session_id=self.old[account.email]['session_id'],
                history_name='campaign_previous_partial.json')
            self.fixture.persist([row])
            self.previous.append(row)
        history = {'status': 'partial', 'items': [{
            'clip_uid': 'fixture-clip', 'task_id': 'fixture-task', 'registry_key': 'minute|fixture-task',
            'accounts': [{'email': row['account_email'], 'org_key': row['org_key'],
                'session_id': row['session_id'], 'ok': True, 'finalized': True}
                for row in self.previous] + [{'email': self.accounts[2].email, 'ok': False,
                    'error': 'Declared fixture failure before journal/CREATE'}]}]}
        (config.DATA_DIR / 'campaign_previous_partial.json').write_text(json.dumps(history), 'utf8')
        sent_registry.mark_sent_many([('minute|fixture-task', 'fixture-clip', account.email)
                                     for account in self.accounts[:2]])
        self.sent = []
        self.events = []

    def deliver_remaining(self, item, account, *_args, **_kwargs):
        self.assertEqual(account.email, self.accounts[2].email,
                         'Previously confirmed recipients must not receive the source again')
        self.assertTrue(self.fixture.video.exists())
        self.sent.append(account.email)
        parent = campaign._terminal_retry_parent(upload.list_sidecars(), account, item, 'fixture-task')
        self.assertEqual(parent, self.old[account.email]['session_id'])
        row = self.fixture.row('confirmed-karina', terminal=False)
        row.update(account_email=account.email, state='done', phase='done', finalized=True,
                   campaign_reconciled=False, evaluation_required=False)
        row['campaign_context'].update(retry_of_session_id=parent, history_name=item['_history_name'])
        self.fixture.persist([row])
        # The declared local sender stands in for successful transfer/finalize.
        # It exercises real journals, history/ACK and cleanup, without HTTP or
        # claiming these inert fixture bytes passed physical media validation.
        upload._remove_sidecar_archive(row['session_id'])
        return {'email': account.email, 'org_key': account.org_key, 'ok': True,
                'finalized': True, 'session_id': row['session_id']}

    def test_only_remaining_account_is_sent_then_three_old_archives_and_shared_video_are_released(self):
        old_journals = {upload._sidecar_path(row['session_id']): None for row in self.old.values()}
        old_journals = {path: path.read_bytes() for path in old_journals}
        previous_journals = {upload._sidecar_path(row['session_id']): None for row in self.previous}
        previous_journals = {path: path.read_bytes() for path in previous_journals}
        old_archives = [row['sidecar_data_path'] for row in self.old.values()]
        clip = {'clip_uid': 'fixture-clip', 'parent_video_uid': 'fixture-source',
                'dur_s': 341.866, 'source': 'ego4d'}
        task = TaskSpec('fixture-task', 'minute|fixture-task', 300, 1800, count=1)
        cfg = CampaignConfig(self.accounts, [task], work_dir=self.fixture.media,
            candidate_plan={task.task_id: [clip]}, account_workers=3, account_gap_s=0,
            shuffle_schedule=False, cleanup_after_upload=True, run_until_exhausted=True)
        prepared = {'video_path': str(self.fixture.video), 'duration_ms': 341866,
                    'imu_real': True, 'source': 'ego4d', '_cleanup_paths': [str(self.fixture.video)]}
        self.assertEqual(campaign._candidate_sent_emails(task.registry_key, clip),
                         {self.accounts[0].email, self.accounts[1].email})
        self.assertEqual(recovery.campaign_exclusions(recovery.snapshot()['items']), {})
        with patch.object(campaign.holoassist, 'data_dir', return_value=self.fixture.root / 'holoassist'), \
             patch.object(campaign, '_clip_is_cached', return_value=True), \
             patch.object(campaign, '_ego_clip_inputs', side_effect=lambda candidate: (candidate, {})), \
             patch.object(campaign, 'prepare_clip', return_value=prepared), \
             patch.object(campaign, 'upload_to_account', side_effect=self.deliver_remaining), \
             patch.object(campaign, '_prefetch_following') as prefetch, \
             patch.object(campaign, '_warm_account_videos') as warm:
            log = campaign.run_campaign(cfg, progress=lambda kind, payload: self.events.append(
                {'type': kind, **payload}))
        self.assertEqual(log.status, 'done')
        self.assertEqual(self.sent, [self.accounts[2].email])
        self.assertFalse(self.fixture.video.exists())
        self.assertTrue(all(not Path(path).exists() for path in old_archives))
        self.assertEqual({path: path.read_bytes() for path in old_journals}, old_journals)
        self.assertEqual({path: path.read_bytes() for path in previous_journals}, previous_journals)
        self.assertTrue(journal_delivery_confirmed(upload.load_sidecar('confirmed-karina')))
        self.assertTrue(upload.load_sidecar('confirmed-karina')['campaign_reconciled'])
        self.assertEqual(sent_registry.sent_emails(task.registry_key, 'fixture-clip'),
                         {account.email for account in self.accounts})
        cleanup_events = [event for event in self.events if event.get('type') == 'storage_cleanup']
        self.assertTrue(cleanup_events)
        self.assertGreaterEqual(cleanup_events[-1]['files'], 5)
        prefetch.assert_not_called()
        warm.assert_not_called()


if __name__ == '__main__':
    unittest.main()
