"""Cross-layer campaign recovery and quality policy, using temporary state only."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from moneymin import config, device_profile, recovery, sent_registry, upload


class _Session:
    email = 'fixture@example.invalid'

    def __init__(self):
        self.calls = []
        self.created = 0

    def ensure_auth(self):
        return {'email': self.email}

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.startswith('/api/v1/uploads?'):
            self.created += 1
            return 201, json.dumps({'id': f'fixture-upload-{self.created}', 'status': 'initiated', 'meta': {}})
        if path == '/api/v1/storage/sas/blobs':
            return 200, json.dumps({'signed_urls': [
                {'filename': item['filename'], 'blob_url': f"https://blob.invalid/{item['filename']}", 'expires_at': '2030-01-01T00:00:00Z'}
                for item in body['files']]})
        if path.endswith('/complete'):
            return 200, json.dumps({'id': path.split('/')[-2], 'status': 'uploaded', 'meta': {}})
        if path.endswith('/finalize'):
            return 204, ''
        raise AssertionError(f'Unexpected fixture operation: {method} {path}')


@contextmanager
def _fixture(count=1):
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
        root = Path(temporary)
        paths = [root / f'chunk{index}.mp4' for index in range(count)]
        for path in paths:
            path.write_bytes(b'Not real media: offline state-machine fixture.')
        recorded = device_profile.format_recorded_at(time.time() - (count + 1) * 60)
        stack.enter_context(patch.object(upload, 'sidecars_dir', return_value=root / 'journals'))
        stack.enter_context(patch.object(upload, '_probe_duration_ms', return_value=60_000))
        stack.enter_context(patch.object(upload, '_put_blob_file', return_value=201))
        stack.enter_context(patch.object(upload, 'probe_video', return_value={
            'duration_ms': 60_000, 'fps': 30, 'width': 1440, 'height': 1080,
            'codec': 'h264', 'has_video': True, 'has_audio': True}))
        yield root, paths, recorded


def _journal(path, recorded, index=0, **changes):
    row = {'session_id': 'fixture-session', 'org_key': 'fixture-org',
           'task_id': 'fixture-task', 'chunk_index': index,
           'log_id': f'fixture-session_{index}', 'filename': f'fixture-session_{index}.mp4',
           'account_email': _Session.email, 'recorded_at': recorded,
           'local_video_path': str(path.resolve()), 'size_bytes': path.stat().st_size,
           'state': upload.STATE_CREATING, 'phase': 'queued', 'attempts': 0,
           'crash_resumes': 0, 'expected_chunk_count': 1, 'finalize_requested': False,
           'register_first': True, **changes}
    upload.save_sidecar(row)


def _send(session, paths, recorded, **options):
    return upload.upload_session(session, paths, 'fixture-org',
        session_id='fixture-session', task_id='fixture-task', recorded_at=recorded,
        normalize=False, sidecar=False, persist_sidecar=True, max_retries=1, **options)


class PersistedQualityPolicyTests(unittest.TestCase):
    def send(self, status, count=1, required_index=0):
        with _fixture(count) as (_, paths, recorded), ExitStack() as stack:
            chunk_recorded = device_profile.format_recorded_at(
                device_profile.recorded_at_to_wall_ms(recorded) / 1000 + required_index * 60)
            _journal(paths[required_index], chunk_recorded, index=required_index,
                     expected_chunk_count=count, evaluation_required=True,
                     evaluation_verified=False, finalize_requested=True)
            session = _Session()
            evaluate = stack.enter_context(patch.object(upload, 'evaluate_upload',
                side_effect=lambda _, uid: {'upload_id': uid, 'checks': [{'id': 'quality', 'status': status}]}))
            remove = stack.enter_context(patch.object(upload, '_remove_sidecar_archive'))
            result = _send(session, paths, recorded, evaluate=False, finalize=True)
            rows = [upload.load_sidecar('fixture-session', index) for index in range(count)]
            return result, rows, evaluate.call_count, remove.call_count, session.calls

    def test_existing_quality_requirement_blocks_finalize_when_evaluation_fails(self):
        result, rows, evaluated, removed, calls = self.send('fail')
        self.assertEqual(evaluated, 1)
        self.assertFalse(result.finalized)
        self.assertEqual(removed, 0)
        self.assertFalse(any(path.endswith('/finalize') for _, path, _ in calls))
        self.assertEqual(rows[0]['state'], upload.STATE_QUARANTINE)
        self.assertIs(rows[0]['evaluation_verified'], False)

    def test_existing_quality_requirement_is_verified_before_successful_finalize(self):
        result, rows, evaluated, _, calls = self.send('pass')
        self.assertEqual(evaluated, 1)
        self.assertTrue(result.finalized)
        self.assertIs(rows[0]['evaluation_verified'], True)
        self.assertEqual(sum(path.endswith('/finalize') for _, path, _ in calls), 1)

    def test_later_required_chunk_cannot_be_bypassed_by_optional_first_chunk(self):
        result, rows, evaluated, removed, calls = self.send('fail', count=2, required_index=1)
        self.assertEqual(evaluated, 1)
        self.assertFalse(result.finalized)
        self.assertEqual(removed, 0)
        self.assertFalse(any(path.endswith('/finalize') for _, path, _ in calls))
        self.assertTrue(all(row['finalized'] is False for row in rows))

    def test_optional_new_upload_does_not_acquire_an_evaluation_requirement(self):
        with _fixture() as (_, paths, recorded), patch.object(upload, 'evaluate_upload') as evaluate:
            result = _send(_Session(), paths, recorded, evaluate=False, finalize=True)
            row = upload.load_sidecar('fixture-session', 0)
        evaluate.assert_not_called()
        self.assertTrue(result.finalized)
        self.assertIs(row['evaluation_required'], False)

    def test_inconclusive_persisted_requirement_is_not_an_approval(self):
        result, rows, evaluated, removed, calls = self.send('pending')
        self.assertEqual(evaluated, 1)
        self.assertFalse(result.finalized)
        self.assertEqual(removed, 0)
        self.assertFalse(any(path.endswith('/finalize') for _, path, _ in calls))
        self.assertEqual(rows[0]['phase'], 'evaluation_review')


class RecoveryCapabilityContractTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.journals = self.root / 'sidecars'
        self.journals.mkdir()
        for mocked in (patch.object(config, 'DATA_DIR', self.root),
                       patch.object(config, 'MEDIA_DATA_DIR', self.root),
                       patch.object(upload, 'sidecars_dir', return_value=self.journals)):
            self.stack.enter_context(mocked)
        self.row = {'session_id': 'existing', 'account_email': 'fixture@example.invalid',
                    'org_key': 'org', 'task_id': 'task', 'chunk_index': 0,
                    'expected_chunk_count': 1, 'state': upload.STATE_TRANSPORT,
                    'phase': 'registered', 'upload_id': 'remote-existing',
                    'finalize_requested': True, 'finalized': False,
                    'campaign_context': {'registry_key': 'task', 'clip_uid': 'clip'}}
        self.path = self.journals / 'existing.json'

    def write(self, changes=None):
        self.path.write_text(json.dumps({**self.row, **(changes or {})}), encoding='utf-8')

    def test_known_receipt_in_unsupported_phase_requires_review_and_remains_reserved(self):
        for phase in ('registered', 'sas', 'sas_ready', 'transport', 'queued', 'unknown'):
            with self.subTest(phase=phase):
                self.write({'phase': phase})
                before = self.path.read_bytes()
                item = recovery.snapshot()['items'][0]
                self.assertFalse(item['can_resume'])
                self.assertEqual(item['status'], 'needs_review')
                self.assertEqual(recovery.campaign_exclusions([item]), {'clip': [self.row['account_email']]})
                with patch.object(upload, 'save_sidecar') as save, patch.object(upload, 'upload_session') as send:
                    with self.assertRaises(upload.UploadError):
                        upload.pump_pending(SimpleNamespace(email=self.row['account_email']))
                save.assert_not_called()
                send.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)

    def test_supported_receipt_phases_keep_automatic_recovery(self):
        for phase in ('transport_done', 'completing', 'complete', 'awaiting_finalize', 'finalize', 'finalizing'):
            with self.subTest(phase=phase):
                self.write({'phase': phase})
                self.assertTrue(recovery.snapshot()['items'][0]['can_resume'])

    def test_invalid_counters_and_exhausted_resume_budget_do_not_offer_resume(self):
        for field, value in (('attempts', True), ('attempts', -1), ('attempts', '1'),
                             ('crash_resumes', None), ('crash_resumes', upload.MAX_CRASH_RESUMES)):
            with self.subTest(field=field, value=value):
                self.write({'phase': 'complete', field: value})
                self.assertFalse(recovery.snapshot()['items'][0]['can_resume'])

    def test_posttransport_state_without_receipt_cannot_be_recreated(self):
        for phase in ('transport_done', 'complete', 'awaiting_finalize'):
            with self.subTest(phase=phase):
                self.write({'state': upload.STATE_COMPLETING, 'phase': phase,
                            'upload_id': None, 'recorded_at': device_profile.format_recorded_at(time.time() - 60)})
                before = self.path.read_bytes()
                self.assertFalse(recovery.snapshot()['items'][0]['can_resume'])
                with patch.object(upload, 'save_sidecar') as save, patch.object(upload, 'upload_session') as send:
                    with self.assertRaises(upload.UploadError):
                        upload.pump_pending(SimpleNamespace(email=self.row['account_email']))
                save.assert_not_called()
                send.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)

    def test_pretransport_without_receipt_requires_original_clock(self):
        for recorded in (None, '', 'invalid'):
            with self.subTest(recorded=recorded):
                self.write({'state': upload.STATE_CREATING, 'phase': 'queued',
                            'upload_id': None, 'recorded_at': recorded})
                self.assertFalse(recovery.snapshot()['items'][0]['can_resume'])
        self.write({'state': upload.STATE_CREATING, 'phase': 'queued', 'upload_id': None,
                    'recorded_at': device_profile.format_recorded_at(time.time() - 60)})
        self.assertTrue(recovery.snapshot()['items'][0]['can_resume'])

    def test_incomplete_sibling_receipt_blocks_the_whole_group_before_checkpoint(self):
        first = {**self.row, 'state': upload.STATE_DONE, 'phase': 'done',
                 'expected_chunk_count': 2, 'upload_id': None}
        second = {**self.row, 'state': upload.STATE_COMPLETING, 'phase': 'awaiting_finalize',
                  'expected_chunk_count': 2, 'chunk_index': 1}
        self.path.write_text(json.dumps(first), encoding='utf-8')
        other = self.journals / 'existing__1.json'
        other.write_text(json.dumps(second), encoding='utf-8')
        before = (self.path.read_bytes(), other.read_bytes())
        self.assertFalse(recovery.snapshot()['items'][0]['can_resume'])
        with patch.object(upload, 'save_sidecar') as save, \
             patch.object(upload, 'complete_upload') as complete, \
             patch.object(upload, '_finalize_session') as finalize:
            with self.assertRaises(upload.UploadError):
                upload.pump_pending(SimpleNamespace(email=self.row['account_email']))
        save.assert_not_called()
        complete.assert_not_called()
        finalize.assert_not_called()
        self.assertEqual((self.path.read_bytes(), other.read_bytes()), before)

    def test_api_rejects_unavailable_recovery_without_starting_worker(self):
        from moneymin.web import server
        self.write()
        worker = Mock(running=False)
        with patch.object(server, 'RECOVERY', worker), \
             patch.object(server, 'RUNNER', SimpleNamespace(running=False)), \
             patch.object(server, 'HOLO_CACHE_RUNNER', SimpleNamespace(running=False)), \
             patch.object(server, '_list_accounts', return_value=[{'email': self.row['account_email']}]):
            client = server.create_app(for_testing=True).test_client()
            result = client.post('/api/recovery/resume', json={'email': self.row['account_email'], 'confirmed': True})
        self.assertEqual(result.status_code, 409)
        worker.start.assert_not_called()

    def test_ambiguous_or_nonfinite_journal_cannot_confirm_or_reconcile_delivery(self):
        good = json.dumps({**self.row, 'state': 'done', 'finalized': True})
        invalid = [good[:-1] + ', "finalized":false, "finalized":true}',
                   good[:-1] + ', "unexpected":NaN}',
                   good[:-1] + ', "unexpected":1e9999}']
        for raw in invalid:
            with self.subTest(raw=raw):
                self.path.write_text(raw, encoding='utf-8')
                before = self.path.read_bytes()
                with patch.object(sent_registry, 'mark_sent_many') as mark:
                    with self.assertRaises(ValueError):
                        recovery.snapshot()
                    with self.assertRaises(ValueError):
                        recovery.reconcile_confirmed()
                mark.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)

    def test_valid_legacy_zero_chunk_bom_remains_readable_without_rewriting(self):
        row = {**self.row, 'state': 'done', 'finalized': True}
        row.pop('chunk_index')
        self.path.write_text(json.dumps(row), encoding='utf-8-sig')
        before = self.path.read_bytes()
        self.assertEqual(len(upload.list_sidecars()), 1)
        self.assertEqual(recovery.snapshot()['confirmed'], 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_directory_io_failure_cannot_look_like_an_empty_recovery_queue(self):
        with patch.object(Path, 'iterdir', side_effect=PermissionError('private-path-secret')):
            with self.assertRaisesRegex(ValueError, 'registros|registro') as raised:
                recovery.snapshot()
        self.assertNotIn('private-path-secret', str(raised.exception))
