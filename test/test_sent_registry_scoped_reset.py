"""An explicit old-campaign reset preserves newer receipts and interrupted work."""
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from moneymin import config, recovery, sent_registry, upload


class ScopedSentResetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.journals = self.root / 'sidecars'
        self.journals.mkdir()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(config, 'DATA_DIR', self.root))
        self.stack.enter_context(patch.object(config, 'MEDIA_DATA_DIR', self.root))
        self.stack.enter_context(patch.object(upload, 'sidecars_dir', return_value=self.journals))
        self.stack.enter_context(patch('socket.socket.connect', side_effect=AssertionError('network forbidden')))

    def receipt(self, email='fixture@example.invalid', *, sid=None, ok=True, finalized=True):
        return {'email': email, 'session_id': sid, 'ok': ok, 'finalized': finalized}

    def item(self, clip, accounts=None, key='minute|task|Task'):
        return {'clip_uid': clip, 'task_id': 'task', 'registry_key': key,
                'accounts': accounts or [self.receipt()]}

    def history(self, name, items):
        path = self.root / name
        path.write_text(json.dumps({'items': items}), encoding='utf8')
        return path

    def journal(self, sid, clip, *, email='fixture@example.invalid', key='minute|task|Task',
                history='campaign_old.json', **updates):
        row = {'session_id': sid, 'account_email': email, 'org_key': 'fixture-org',
               'task_id': 'task', 'chunk_index': 0, 'expected_chunk_count': 1,
               'upload_id': f'accepted-{sid}', 'state': 'done', 'finalized': True,
               'campaign_context': {'registry_key': key, 'clip_uid': clip,
                                    'task_id': 'task', 'history_name': history}}
        row.update(updates)
        index = row['chunk_index']
        name = f'{sid}.json' if index == 0 else f'{sid}__{index}.json'
        path = self.journals / name
        path.write_text(json.dumps(row), encoding='utf8')
        return path

    def snapshots(self):
        return {path.relative_to(self.root): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file() and not path.name.endswith('.lock')}

    def assert_preserved_except_index(self, before):
        after = self.snapshots()
        for name, data in before.items():
            if name.name not in {'sent_videos.json', 'sent_reset_history.json'}:
                self.assertEqual(after[name], data)

    def test_old_reset_rebuilds_41_new_receipts_and_preserves_three_pending(self):
        self.history('campaign_old.json', [self.item('old', [self.receipt(sid='old')])])
        self.journal('old', 'old')
        current = []
        for index in range(44):
            email = f'account-{index}@example.invalid'
            sid = f'current{index}'
            confirmed = index < 41
            current.append(self.receipt(email, sid=sid, ok=confirmed, finalized=confirmed))
            self.journal(sid, 'current', email=email, history='campaign_current.json',
                         state='done' if confirmed else 'transport', finalized=confirmed,
                         phase='done' if confirmed else 'sas_ready')
        self.history('campaign_current.json', [self.item('current', current)])
        sent_registry._save({'minute|task|Old label': {'old': ['fixture@example.invalid']}})
        before = self.snapshots()
        sent_registry.reset(history_names=['campaign_old.json'])
        self.assertEqual(sent_registry.recorded_emails(sent_registry.load(), 'minute|task|Task', 'old'), set())
        self.assertEqual(sent_registry.recorded_emails(sent_registry.load(), 'minute|task|Task', 'current'),
                         {f'account-{index}@example.invalid' for index in range(41)})
        resets = sent_registry._reset_history()
        self.assertEqual(resets['all'], ['campaign_old.json'])
        self.assertEqual(resets['completed_sessions'], ['old'])
        result = recovery.snapshot()
        self.assertEqual(result['confirmed'], 41)
        self.assertEqual(result['pending'], 3)
        self.assertEqual(set(recovery.campaign_exclusions(result['items'])['current']),
                         {f'account-{index}@example.invalid' for index in range(44)})
        self.assert_preserved_except_index(before)
        # Losing the cache index later must neither resurrect old receipts nor
        # release the latest confirmed deliveries.
        (self.root / sent_registry.FILE_NAME).unlink()
        self.assertNotIn('old', sent_registry.load()['minute|task|Task'])
        self.assertEqual(len(sent_registry.load()['minute|task|Task']['current']), 41)

    def test_selected_history_pending_pair_and_reservation_are_not_released(self):
        self.history('campaign_old.json', [self.item('same', [self.receipt(sid='old')])])
        self.journal('old', 'same')
        self.journal('pending', 'same', state='transport', phase='sas_ready', finalized=False)
        sent_registry._save({'minute|task|Task': {'same': ['fixture@example.invalid']}})
        before = self.snapshots()
        sent_registry.reset(history_names=['campaign_old.json'])
        self.assertEqual(sent_registry.sent_emails('minute|task|Task', 'same'), {'fixture@example.invalid'})
        self.assertEqual(sent_registry._reset_history()['completed_sessions'], ['old'])
        result = recovery.snapshot()
        self.assertEqual(result['pending'], 1)
        self.assertEqual(recovery.campaign_exclusions(result['items']), {'same': ['fixture@example.invalid']})
        self.assert_preserved_except_index(before)

    def test_null_finalization_is_pending_evidence_and_not_an_invalid_history(self):
        self.history('campaign_old.json', [self.item('pending', [self.receipt(sid='pending', finalized=None)]),
                                          self.item('legacy', [{'email': 'fixture@example.invalid', 'ok': True}])])
        sent_registry._save({'minute|task|Task': {'pending': ['fixture@example.invalid'],
                                                 'legacy': ['fixture@example.invalid']}})
        sent_registry.reset(history_names=['campaign_old.json'])
        self.assertEqual(sent_registry.sent_emails('minute|task|Task', 'pending'), {'fixture@example.invalid'})
        self.assertEqual(sent_registry.sent_emails('minute|task|Task', 'legacy'), set())

    def test_new_history_label_alias_and_unrelated_ledger_entries_remain_sent(self):
        self.history('campaign_old.json', [self.item('same', key='minute|task|Old label')])
        self.history('campaign_current.json', [self.item('same', key='minute|task|New label')])
        sent_registry._save({'minute|task|Old label': {'same': ['fixture@example.invalid']},
                             'unrelated': {'clip': ['another@example.invalid']}})
        sent_registry.reset(history_names=['campaign_old.json'])
        data = sent_registry.load()
        self.assertEqual(sent_registry.recorded_emails(data, 'minute|task|Any label', 'same'),
                         {'fixture@example.invalid'})
        self.assertEqual(data['unrelated'], {'clip': ['another@example.invalid']})

    def test_legacy_configuration_without_items_is_preserved_and_proves_no_deliveries(self):
        self.history('campaign_old.json', [self.item('old')])
        self.history('campaign_current.json', [self.item('current')])
        auxiliary = self.root / 'campaign_test_anticollusion.json'
        payload = {'work_dir': 'fixture-media', 'timeout_blob': 1200, 'evaluate': True,
                   'finalize': True, 'delay_mode': 'off', 'account_gap_s': 0,
                   'active_hours': None, 'accounts': [{'email': 'fixture@example.invalid',
                                                      'org_key': 'fixture-org'}],
                   'tasks': [{'task_id': 'task', 'scenario': 'Task', 'min_dur_s': 300,
                              'max_dur_s': 1800, 'count': 1}]}
        auxiliary.write_text(json.dumps(payload), encoding='utf8')
        before = auxiliary.read_bytes()
        self.assertEqual(sent_registry._history_deliveries(auxiliary), [])
        sent_registry.reset(history_names=['campaign_old.json'])
        self.assertEqual(sent_registry.load(), {'minute|task|Task': {'current': ['fixture@example.invalid']}})
        self.assertEqual(auxiliary.read_bytes(), before)
        for invalid in ({**payload, 'items': None}, {**payload, 'items': {}},
                        {**payload, 'started_at': '2026-10-06T00:00:00Z'},
                        {**payload, 'status': 'done'}, {**payload, 'accounts': None},
                        {**payload, 'tasks': None}, {**payload, 'work_dir': None}):
            with self.subTest(invalid=invalid):
                auxiliary.write_text(json.dumps(invalid), encoding='utf8')
                before = self.snapshots()
                with patch.object(sent_registry, 'save_json', side_effect=AssertionError('reset write')), \
                     patch.object(sent_registry, '_save', side_effect=AssertionError('index write')):
                    with self.assertRaises(ValueError):
                        sent_registry.reset(history_names=['campaign_old.json'])
                self.assertEqual(self.snapshots(), before)

    def test_scenario_selector_resets_alias_receipts_without_resurrection(self):
        self.history('campaign_old.json', [self.item('old', key='minute|task|Previous label'),
                                           self.item('other', key='other-task')])
        sent_registry.reset('minute|task|Current label', history_names=['campaign_old.json'])
        self.assertEqual(sent_registry.load(), {'other-task': {'other': ['fixture@example.invalid']}})
        (self.root / sent_registry.FILE_NAME).unlink()
        self.assertEqual(sent_registry.load(), {'other-task': {'other': ['fixture@example.invalid']}})

    def test_unselected_completed_context_is_retained_without_history_receipt(self):
        self.history('campaign_old.json', [self.item('old')])
        self.journal('unselected', 'old', history='campaign_current.json')
        sent_registry.reset(history_names=['campaign_old.json'])
        self.assertEqual(sent_registry.sent_emails('minute|task|Task', 'old'), {'fixture@example.invalid'})
        self.assertEqual(sent_registry._reset_history()['completed_sessions'], [])

    def test_incomplete_or_unverified_old_session_is_never_reset(self):
        for updates in ({'expected_chunk_count': 2}, {'finalized': False, 'state': 'transport'},
                        {'upload_id': None}, {'evaluation_required': True, 'evaluation_verified': False},
                        {'finalized': 1}):
            with self.subTest(updates=updates):
                self.history('campaign_old.json', [self.item('old', [self.receipt(sid='old')])])
                self.journal('old', 'old', **updates)
                sent_registry._save({'minute|task|Task': {'old': ['fixture@example.invalid']}})
                sent_registry.reset(history_names=['campaign_old.json'])
                self.assertEqual(sent_registry.sent_emails('minute|task|Task', 'old'), {'fixture@example.invalid'})
                self.assertEqual(sent_registry._reset_history()['completed_sessions'], [])

    def test_mixed_or_missing_organization_preserves_old_pair_and_session(self):
        for organizations in (('org-one', 'org-two'), (None, None), ('', ''), (' ', ' ')):
            with self.subTest(organizations=organizations):
                self.history('campaign_old.json', [self.item('old', [self.receipt(sid='old')])])
                self.journal('old', 'old', expected_chunk_count=2, org_key=organizations[0])
                self.journal('old', 'old', expected_chunk_count=2, chunk_index=1, org_key=organizations[1])
                sent_registry._save({'minute|task|Task': {'old': ['fixture@example.invalid']}})
                before = self.snapshots()
                sent_registry.reset(history_names=['campaign_old.json'])
                self.assertEqual(sent_registry.sent_emails('minute|task|Task', 'old'), {'fixture@example.invalid'})
                self.assertEqual(sent_registry._reset_history()['completed_sessions'], [])
                self.assert_preserved_except_index(before)

    def test_ambiguous_legacy_session_or_wrong_task_does_not_release_history_pairs(self):
        for items, updates in (([self.item('one', [self.receipt(sid='old')]),
                                 self.item('two', [self.receipt(sid='old')])], {'campaign_context': None}),
                               ([self.item('one', [self.receipt(sid='old')])],
                                {'campaign_context': None, 'task_id': 'another-task'})):
            with self.subTest(updates=updates):
                self.history('campaign_old.json', items)
                self.journal('old', 'one', **updates)
                sent_registry._save({'minute|task|Task': {'one': ['fixture@example.invalid'],
                                                         'two': ['fixture@example.invalid']}})
                sent_registry.reset(history_names=['campaign_old.json'])
                self.assertEqual(sent_registry._reset_history()['completed_sessions'], [])
                self.assertEqual(sent_registry.sent_emails('minute|task|Task', 'one'), {'fixture@example.invalid'})

    def test_blank_organization_stays_blocked_after_scoped_history_reset(self):
        self.history('campaign_old.json', [self.item('old', [self.receipt(sid='old')])])
        journal = self.journal('old', 'old', org_key=' ')
        before = journal.read_bytes()
        sent_registry._save({'minute|task|Task': {'old': ['fixture@example.invalid']}})
        with self.assertRaises(recovery.RecoveryReadError):
            recovery.snapshot()
        sent_registry.reset(history_names=['campaign_old.json'])
        with self.assertRaises(recovery.RecoveryReadError):
            recovery.snapshot()
        self.assertEqual(sent_registry.sent_emails('minute|task|Task', 'old'), {'fixture@example.invalid'})
        self.assertEqual(sent_registry._reset_history()['completed_sessions'], [])
        self.assertEqual(journal.read_bytes(), before)

    def test_invalid_selection_history_or_journal_aborts_before_json_writes(self):
        history = self.history('campaign_old.json', [self.item('old')])
        sent_registry._save({'minute|task|Task': {'old': ['fixture@example.invalid']}})
        for names in ('campaign_old.json', ['../campaign_old.json'], ['campaign_missing.json'], [42]):
            with self.subTest(names=names):
                before = self.snapshots()
                with patch.object(sent_registry, 'save_json', side_effect=AssertionError('reset write')), \
                     patch.object(sent_registry, '_save', side_effect=AssertionError('index write')):
                    with self.assertRaises(ValueError):
                        sent_registry.reset(history_names=names)
                self.assertEqual(self.snapshots(), before)
        for payload in ('{', '{"items": [] , "items": []}',
                        '{"items":[{"clip_uid":"old","accounts":[]}]}',
                        '{"items":[{"clip_uid":"old","registry_key":"task","accounts":[{"email":"x","ok":1}]}]}'):
            with self.subTest(payload=payload):
                history.write_text(payload, encoding='utf8')
                before = self.snapshots()
                with patch.object(sent_registry, 'save_json', side_effect=AssertionError('reset write')), \
                     patch.object(sent_registry, '_save', side_effect=AssertionError('index write')):
                    with self.assertRaises(ValueError):
                        sent_registry.reset(history_names=['campaign_old.json'])
                self.assertEqual(self.snapshots(), before)
        self.history('campaign_old.json', [self.item('old')])
        for payload in ('{', '{"session_id":"mismatched","chunk_index":0}'):
            with self.subTest(journal=payload):
                (self.journals / 'broken.json').write_text(payload, encoding='utf8')
                before = self.snapshots()
                with patch.object(sent_registry, 'save_json', side_effect=AssertionError('reset write')), \
                     patch.object(sent_registry, '_save', side_effect=AssertionError('index write')):
                    with self.assertRaises(ValueError):
                        sent_registry.reset(history_names=['campaign_old.json'])
                self.assertEqual(self.snapshots(), before)

    def test_empty_selection_is_noop_and_scoped_reset_never_migrates_or_reads_accounts(self):
        self.history('campaign_old.json', [self.item('old')])
        with patch.object(upload, 'list_sidecars', side_effect=AssertionError('migration forbidden')), \
             patch.object(upload, 'sidecars_dir', side_effect=AssertionError('migration forbidden')), \
             patch.object(upload, 'save_sidecar', side_effect=AssertionError('journal write forbidden')):
            before = self.snapshots()
            sent_registry.reset(history_names=[])
            self.assertEqual(self.snapshots(), before)
            sent_registry.reset(history_names=['campaign_old.json'])
        self.assertEqual(sent_registry.load(), {})


if __name__ == '__main__':
    unittest.main()
