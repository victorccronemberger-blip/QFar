"""Original attempt, current receipts and preview readiness stay independent."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moneymin import config, recovery, upload
from moneymin.web import server


class CampaignCurrentEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='campaign-current-evidence-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.journals = self.root / 'journals'
        self.journals.mkdir()
        for mocked in (patch.object(config, 'DATA_DIR', self.root),
                       patch.object(config, 'MEDIA_DATA_DIR', self.root),
                       patch.object(upload, 'sidecars_dir', return_value=self.journals),
                       patch.object(server.Session, 'from_email', side_effect=AssertionError('no auth on local history'))):
            mocked.start()
            self.addCleanup(mocked.stop)
        self.client = server.create_app(for_testing=True).test_client()
        self.path = self.root / 'campaign_witness.json'
        self.history = {'status': 'error', 'accounts': ['fixture@example.com'], 'items': [{
            'clip_uid': 'clip', 'registry_key': 'registry', 'task_id': 'task',
            'accounts': [{'email': 'fixture@example.com', 'org_key': 'org',
                          'session_id': 'session', 'ok': False, 'finalized': False,
                          'error': 'finalize returned 503', 'uploads': ['upload-0']}]}]}
        self.row = {'session_id': 'session', 'account_email': 'fixture@example.com', 'org_key': 'org',
                    'task_id': 'task', 'chunk_index': 0, 'expected_chunk_count': 1,
                    'upload_id': 'upload-0', 'state': 'done', 'phase': 'done', 'finalized': True,
                    'evaluation_required': True, 'evaluation_verified': True,
                    'campaign_reconciled': True, 'campaign_context': {
                        'clip_uid': 'clip', 'registry_key': 'registry', 'task_id': 'task',
                        'history_name': self.path.name}, 'token': 'PRIVATE_SENTINEL',
                    'blob_url': 'PRIVATE_SENTINEL', 'local_video_path': 'PRIVATE_SENTINEL'}
        self.write_history()

    def write_history(self):
        self.path.write_text(json.dumps(self.history), encoding='utf-8')

    def write_row(self, row=None):
        row = self.row if row is None else row
        path = self.journals / upload._sidecar_filename(row['session_id'], row.get('chunk_index', 0))
        path.write_text(json.dumps(row), encoding='utf-8')
        return path

    def view(self):
        response = self.client.get('/api/logs/' + self.path.name)
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def current(self):
        return self.view()['items'][0]['accounts'][0]['current_result']

    def test_current_confirmed_receipt_keeps_original_failed_attempt_and_bytes(self):
        journal = self.write_row()
        before = {path: path.read_bytes() for path in (self.path, journal)}
        self.assertEqual(recovery.snapshot()['items'], [])
        view = self.view()
        original = view['items'][0]['accounts'][0]
        self.assertEqual(view['status'], 'error')
        self.assertEqual(original['status'], 'failed')
        self.assertEqual(original['confirmation'], 'not_confirmed')
        self.assertEqual(view['summary']['success'], 0)
        self.assertEqual(view['summary']['failed'], 1)
        self.assertEqual(view['current_summary']['confirmed'], 1)
        self.assertEqual(original['current_result']['status'], 'confirmed')
        self.assertEqual(original['current_result']['evidence_source'], 'journal')
        self.assertIs(original['current_result']['reconciled'], True)
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(view))
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_absent_journal_is_unknown_even_when_preview_would_be_ready(self):
        self.assertEqual(self.current()['status'], 'unknown')
        self.assertEqual(self.view()['current_summary']['confirmed'], 0)

    def test_confirmation_requires_all_parts_and_consistent_identity(self):
        self.row['expected_chunk_count'] = 2
        self.write_row()
        self.assertNotEqual(self.current()['status'], 'confirmed')
        second = {**self.row, 'chunk_index': 1, 'upload_id': 'upload-1'}
        self.write_row(second)
        self.assertEqual(self.current()['status'], 'confirmed')
        for field, value in [('org_key', 'other'), ('account_email', 'other@example.com'),
                             ('task_id', 'other'), ('expected_chunk_count', 3)]:
            with self.subTest(field=field):
                self.write_row({**second, field: value})
                self.assertNotEqual(self.current()['status'], 'confirmed')
        self.write_row(second)
        for field, value in [('clip_uid', 'other'), ('registry_key', 'other'),
                             ('task_id', 'other'), ('history_name', 'other.json')]:
            with self.subTest(context=field):
                self.write_row({**second, 'campaign_context': {**second['campaign_context'], field: value}})
                self.assertNotEqual(self.current()['status'], 'confirmed')

    def test_unmet_quality_and_untyped_flags_never_prove_confirmation(self):
        for changes in ({'evaluation_verified': False}, {'phase': 'evaluation_review'},
                        {'finalized': 'true'}, {'evaluation_required': 1}, {'state': 'completing'}):
            with self.subTest(changes=changes):
                self.write_row({**self.row, **changes})
                self.assertNotEqual(self.current()['status'], 'confirmed')

    def test_invalid_upload_identifiers_never_confirm_a_current_receipt(self):
        for value in (None, '', '   ', 42):
            with self.subTest(value=value):
                row = {**self.row, 'upload_id': value}
                if value is None:
                    row.pop('upload_id')
                self.write_row(row)
                self.assertNotEqual(self.current()['status'], 'confirmed')
                self.assertEqual(self.view()['current_summary']['confirmed'], 0)

    def test_explicit_unknown_legacy_finalization_remains_pending(self):
        self.history['items'][0]['accounts'][0].update(ok=True, finalized=None)
        self.write_history()
        view = self.view()
        self.assertEqual(view['items'][0]['accounts'][0]['status'], 'pending')
        self.assertEqual(view['summary']['success'], 0)

    def test_strict_duplicate_and_nonfinite_journal_cannot_confirm_or_leak(self):
        journal = self.write_row()
        for raw in (json.dumps(self.row)[:-1] + ',"finalized":false}',
                    json.dumps(self.row)[:-1] + ',"value":NaN}'):
            with self.subTest(raw=raw[-24:]):
                journal.write_text(raw, encoding='utf-8')
                before = journal.read_bytes()
                current = self.current()
                self.assertNotEqual(current['status'], 'confirmed')
                self.assertEqual(journal.read_bytes(), before)
                self.assertNotIn('PRIVATE_SENTINEL', json.dumps(current))

    def test_preview_ready_does_not_confirm_failed_delivery(self):
        self.history['status'] = 'partial'
        self.history['items'][0]['accounts'].append({
            'email': 'fixture@example.com', 'org_key': 'org', 'session_id': 'confirmed',
            'ok': True, 'finalized': True, 'uploads': ['other-upload']})
        self.write_history()
        self.write_row({**self.row, 'session_id': 'confirmed', 'upload_id': 'other-upload'})
        with patch.object(server.Session, 'from_email', return_value=object()), \
             patch.object(server.campaign, 'session_result', return_value={
                 'status': 'preview_ready', 'ready_files': 1, 'total_files': 1}):
            response = self.client.post('/api/logs/' + self.path.name + '/status')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['summary']['ready'], 2)
        self.assertEqual(data['delivery_summary']['confirmed'], 1)
        self.assertEqual(data['delivery_summary']['unknown'], 1)
        self.assertFalse(data['all_deliveries_confirmed'])
        self.assertEqual(data['attempt_status'], 'partial')

    def test_original_confirmation_does_not_invent_a_current_journal(self):
        attempt = self.history['items'][0]['accounts'][0]
        attempt.update(ok=True, finalized=True)
        self.write_history()
        view = self.view()
        self.assertEqual(view['summary']['success'], 1)
        self.assertEqual(view['items'][0]['accounts'][0]['confirmation'], 'remote_ack')
        self.assertEqual(view['current_summary']['confirmed'], 0)
        self.assertEqual(view['delivery_summary']['unknown'], 1)

    def test_untyped_original_finalization_is_rejected_before_any_remote_check(self):
        for field in ('ok', 'finalized', 'skipped', 'recovered'):
            with self.subTest(field=field):
                history = copy.deepcopy(self.history)
                history['items'][0]['accounts'][0][field] = 'false'
                self.path.write_text(json.dumps(history), encoding='utf-8')
                self.assertEqual(self.client.get('/api/logs/' + self.path.name).status_code, 400)
                self.assertEqual(self.client.post('/api/logs/' + self.path.name + '/status').status_code, 400)

    def test_recovery_worker_reports_reconciled_receipts_separately(self):
        worker = recovery.RecoveryRunner()
        with patch.object(recovery, 'resume_account', return_value={
                'items': [], 'reconciled': 1, 'reconciled_sessions': [{
                    'email': 'fixture@example.com', 'session_id': 'session', 'clip_uid': 'clip'}]}):
            worker._run('fixture@example.com', lambda _: 'org')
        state = worker.snapshot()
        self.assertEqual(state['state'], 'done')
        self.assertEqual(state['result']['reconciled'], 1)
        self.assertEqual(state['result']['reconciled_sessions'][0]['session_id'], 'session')


if __name__ == '__main__':
    unittest.main()
