"""Interrupt durable receipt commits without recreating an acknowledged upload."""
from contextlib import ExitStack
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from moneymin import campaign, campaign_evidence, config, recovery, sent_registry, upload
from moneymin.campaign_types import AccountSpec


class CampaignReceiptCommitTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.journals = self.root / 'journals'
        self.journals.mkdir()
        for context in (patch.object(config, 'DATA_DIR', self.root),
                        patch.object(config, 'MEDIA_DATA_DIR', self.root),
                        patch.object(upload, 'sidecars_dir', return_value=self.journals),
                        patch.object(campaign, '_new_identity', side_effect=AssertionError('no CREATE identity')),
                        patch.object(campaign, 'upload_session', side_effect=AssertionError('no provider upload'))):
            self.stack.enter_context(context)
        self.rows = [{'session_id': 'commit-session', 'chunk_index': index,
                      'expected_chunk_count': 2, 'upload_id': f'accepted-upload-{index}',
                      'account_email': 'commit@example.invalid', 'org_key': 'fixture-org',
                      'task_id': 'fixture-task', 'state': 'done', 'phase': 'done', 'finalized': True,
                      'evaluation_required': True, 'evaluation_verified': True,
                      'campaign_context': {'registry_key': 'fixture-key', 'clip_uid': 'fixture-clip',
                                           'task_id': 'fixture-task'}} for index in range(2)]
        for row in self.rows:
            upload.save_sidecar(row)

    def assert_complete(self):
        result = recovery.reconcile_confirmed()
        self.assertEqual(result['reconciled'], 1)
        self.assertEqual([(row['status'], row['index_reconciled'], row['publication_pending'], row['can_resume'])
                          for row in result['items']], [('confirmed', True, True, False)])
        self.assertEqual(sent_registry.sent_emails('fixture-key', 'fixture-clip'), {'commit@example.invalid'})
        for index, original in enumerate(self.rows):
            actual = upload.load_sidecar('commit-session', index)
            self.assertIs(actual.pop('campaign_reconciled'), True)
            self.assertEqual(actual, original, 'only local acknowledgment may change')
        self.assertEqual(recovery.reconcile_confirmed()['reconciled'], 0)

    def test_index_commit_failure_precedes_every_journal_acknowledgment(self):
        before = {path.name: path.read_bytes() for path in self.journals.iterdir()}
        with patch.object(sent_registry, '_save', side_effect=OSError('fixture index interruption')), \
             patch.object(recovery, 'save_json', wraps=recovery.save_json) as ack:
            with self.assertRaises(OSError):
                recovery.reconcile_confirmed()
            ack.assert_not_called()
        self.assertEqual({path.name: path.read_bytes() for path in self.journals.iterdir()}, before)
        self.assertFalse(sent_registry.sent_emails('fixture-key', 'fixture-clip'))
        self.assertEqual(recovery.snapshot()['confirmed'], 1)
        self.assert_complete()

    def interrupted_ack(self, failing_index):
        real_save = recovery.save_json
        calls = []

        def save(path, row):
            calls.append(row['chunk_index'])
            if row['chunk_index'] == failing_index:
                raise OSError('fixture acknowledgment interruption')
            return real_save(path, row)

        with patch.object(recovery, 'save_json', side_effect=save):
            with self.assertRaises(OSError):
                recovery.reconcile_confirmed()
        self.assertEqual(calls, list(range(failing_index + 1)))
        self.assertEqual(sent_registry.sent_emails('fixture-key', 'fixture-clip'), {'commit@example.invalid'})
        self.assertEqual(recovery.snapshot()['confirmed'], 1)
        current = campaign_evidence.current_result(
            {'task_id': 'fixture-task', 'registry_key': 'fixture-key', 'clip_uid': 'fixture-clip'},
            {'email': 'commit@example.invalid', 'org_key': 'fixture-org', 'session_id': 'commit-session'},
            None, campaign_evidence.current_groups())
        self.assertEqual(current['status'], 'confirmed')
        self.assertIs(current['reconciled'], False)
        self.assert_complete()

    def test_interrupted_first_chunk_ack_repeats_only_local_commits(self):
        self.interrupted_ack(0)

    def test_interrupted_second_chunk_ack_preserves_first_ack_and_receipt_identity(self):
        self.interrupted_ack(1)

    def test_campaign_reconciliation_after_partial_ack_keeps_original_session(self):
        real_save = campaign.save_sidecar

        def save(row):
            if row['chunk_index'] == 1:
                raise OSError('fixture campaign acknowledgment interruption')
            return real_save(row)

        account = AccountSpec('commit@example.invalid', 'fixture-org')
        item = {'registry_key': 'fixture-key', 'clip_uid': 'fixture-clip'}
        with patch.object(campaign, 'save_sidecar', side_effect=save):
            with self.assertRaises(OSError):
                campaign._reconcile_uploads(copy.deepcopy(self.rows), account, item, 'fixture-task')
        persisted = upload.list_sidecars()
        self.assertIs(persisted[0]['campaign_reconciled'], True)
        self.assertNotIn('campaign_reconciled', persisted[1])
        replay = campaign._reconcile_uploads(persisted, account, item, 'fixture-task')
        self.assertIs(replay['ok'], True)
        self.assertEqual(replay['session_id'], 'commit-session')
        self.assertEqual([(row['status'], row['index_reconciled'], row['publication_pending'], row['can_resume'])
                          for row in recovery.snapshot()['items']], [('confirmed', True, True, False)])
        self.assertEqual({row['upload_id'] for row in upload.list_sidecars()},
                         {'accepted-upload-0', 'accepted-upload-1'})
