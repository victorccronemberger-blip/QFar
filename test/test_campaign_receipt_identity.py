"""Campaign/recovery/history must agree on durable upload receipt identity."""
from contextlib import ExitStack
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from moneymin import campaign, campaign_evidence, config, recovery, sent_registry, upload
from moneymin.campaign_types import AccountSpec


class CampaignReceiptIdentityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.journals = self.root / 'journals'
        self.journals.mkdir()
        for context in (patch.object(config, 'DATA_DIR', self.root),
                        patch.object(config, 'MEDIA_DATA_DIR', self.root),
                        patch.object(upload, 'sidecars_dir', return_value=self.journals)):
            self.stack.enter_context(context)
        self.account = AccountSpec('receipt@example.invalid', 'fixture-org')
        self.item = {'registry_key': 'fixture-key', 'clip_uid': 'fixture-clip', 'task_id': 'fixture-task'}
        self.attempt = {'email': self.account.email, 'org_key': self.account.org_key,
                        'session_id': 'fixture-session'}

    def rows(self, count=1, **changes):
        return [{'session_id': 'fixture-session', 'account_email': self.account.email,
                 'org_key': self.account.org_key, 'task_id': 'fixture-task',
                 'chunk_index': index, 'expected_chunk_count': count,
                 'upload_id': f'fixture-upload-{index}', 'state': 'done', 'phase': 'done',
                 'finalized': True, 'evaluation_required': False,
                 'campaign_context': {'registry_key': 'fixture-key', 'clip_uid': 'fixture-clip',
                                      'task_id': 'fixture-task'}, **changes}
                for index in range(count)]

    def persist(self, rows):
        for row in rows:
            upload.save_sidecar(row)
        return {path.name: path.read_bytes() for path in self.journals.iterdir()}

    def assert_unconfirmed(self, rows):
        before = self.persist(rows)
        with patch.object(campaign, '_new_identity', side_effect=AssertionError('no new session')), \
             patch.object(campaign, 'upload_session', side_effect=AssertionError('no network')):
            result = campaign._reconcile_uploads(copy.deepcopy(rows), self.account, self.item, 'fixture-task')
        self.assertIsNotNone(result)
        self.assertFalse(result['ok'], 'campaign must not credit an invalid receipt')
        self.assertIs(result['finalized'], False)
        self.assertFalse(sent_registry.sent_emails('fixture-key', 'fixture-clip'))
        snap = recovery.snapshot()
        self.assertEqual(snap['confirmed'], 0)
        self.assertEqual(len(snap['items']), 1, 'uncertain receipts remain visible and reserved')
        self.assertEqual(snap['items'][0]['status'], 'needs_review')
        self.assertFalse(snap['items'][0]['can_resume'])
        self.assertEqual(recovery.campaign_exclusions(snap['items']),
                         {'fixture-clip': [self.account.email]})
        replay = recovery.reconcile_confirmed()
        self.assertEqual(replay['reconciled'], 0)
        self.assertFalse(sent_registry.sent_emails('fixture-key', 'fixture-clip'))
        current = campaign_evidence.current_result(self.item, self.attempt, None,
                                                   campaign_evidence.current_groups())
        self.assertEqual(current['status'], 'review')
        self.assertEqual({path.name: path.read_bytes() for path in self.journals.iterdir()}, before)

    def test_missing_or_invalid_upload_id_cannot_confirm_in_any_consumer(self):
        for value in (None, '', '   ', 7, True, [], {}):
            with self.subTest(value=value):
                rows = self.rows(upload_id=value)
                if value is None:
                    rows[0].pop('upload_id')
                self.assert_unconfirmed(rows)

    def test_invalid_later_chunk_prevents_group_confirmation(self):
        for value in (None, '', '   ', 7, True):
            with self.subTest(value=value):
                rows = self.rows(2)
                rows[1]['upload_id'] = value
                self.assert_unconfirmed(rows)

    def test_invalid_reconciled_receipt_is_still_visible(self):
        self.assert_unconfirmed(self.rows(upload_id='', campaign_reconciled=True))

    def test_valid_two_chunk_receipt_reconciles_without_new_network_or_identity(self):
        rows = self.rows(2)
        self.persist(rows)
        result = campaign._reconcile_uploads(rows, self.account, self.item, 'fixture-task')
        self.assertTrue(result['ok'])
        self.assertIs(result['finalized'], True)
        self.assertEqual(sent_registry.sent_emails('fixture-key', 'fixture-clip'), {self.account.email})
        self.assertEqual([(row['status'], row['index_reconciled'], row['publication_pending'], row['can_resume'])
                          for row in recovery.snapshot()['items']], [('confirmed', True, True, False)])
        current = campaign_evidence.current_result(self.item, self.attempt, None,
                                                   campaign_evidence.current_groups())
        self.assertEqual(current['status'], 'confirmed')
        self.assertIs(current['reconciled'], True)
