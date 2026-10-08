"""Verified terminal failures release reservations without losing pending bytes."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from moneymin import campaign_evidence, config, content_provenance, recovery, sent_registry, upload
from moneymin import media_lifecycle


class TerminalFailedProtectionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(
            prefix='qmoney-terminal-protection-'))).resolve()
        self.media = self.root / 'media'
        self.media.mkdir()
        self.journals = self.root / 'state' / 'sidecars'
        self.journals.mkdir(parents=True)
        self.stack.enter_context(patch.multiple(config, DATA_DIR=self.root / 'state',
            MEDIA_DATA_DIR=self.media, SECRETS_DIR=self.root / 'secrets'))
        self.stack.enter_context(patch.object(upload, 'sidecars_dir', return_value=self.journals))
        self.stack.enter_context(patch('socket.socket.connect',
            side_effect=AssertionError('network forbidden')))
        self.video = self.media / 'owned.mp4'
        self.video.write_bytes(b'Inert verified provider-owned video bytes')
        self.digest = hashlib.sha256(self.video.read_bytes()).hexdigest()
        media_lifecycle.record_managed_media(self.video, root=self.media,
            provider='ego4d', role='prepared_video', expected_digest=self.digest)

    def row(self, sid='terminal-fixture', index=0, count=1, *, terminal=True):
        archive = self.journals / upload._sidecar_filename(sid, index)
        archive = archive.with_suffix('.data.zip')
        archive.write_bytes(f'Inert original archive {sid} {index}'.encode())
        row = {'session_id': sid, 'chunk_index': index, 'expected_chunk_count': count,
            'account_email': 'fixture@example.invalid', 'org_key': 'fixture-org',
            'task_id': 'fixture-task', 'upload_id': f'fixture-upload-{sid}-{index}',
            'log_id': f'{sid}_{index}', 'filename': f'{sid}_{index}.mp4',
            'recorded_at': '2026-10-08T04:10:30.988Z', 'duration_ms': 341_866,
            'state': 'failed', 'phase': 'complete', 'finalized': False,
            'register_first': True, 'native_response_schema': True,
            'create_attempted': True, 'finalize_requested': True,
            'size_bytes': self.video.stat().st_size,
            'local_video_path': str(self.video), 'video_content_sha256': self.digest,
            'sidecar_data_path': str(archive), 'sidecar_size_bytes': archive.stat().st_size,
            'sidecar_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
            'error': 'Complete rejected the previously failed remote receipt.',
            'campaign_context': {'registry_key': 'minute|fixture-task',
                'clip_uid': 'fixture-clip', 'task_id': 'fixture-task'}}
        if terminal:
            row['phase'] = 'remote_terminal_failure'
            row['remote_terminal_failure'] = {'version': 1, 'status': 'failed',
                'upload_id': row['upload_id'], 'session_id': sid, 'log_id': row['log_id'],
                'email': row['account_email'], 'user_resource_key': 'fixture-user-resource',
                'org_key': row['org_key'],
                'task_id': row['task_id'], 'recorded_at': row['recorded_at'],
                'duration_ms': row['duration_ms'], 'chunk_index': index,
                'expected_chunk_count': count, 'checked_at': '2026-10-08T05:10:30.988Z'}
        return row

    def persist(self, rows):
        for row in rows:
            upload.save_sidecar(row)

    def evidence_bytes(self):
        return {path: path.read_bytes() for path in self.journals.iterdir()
                if path.suffix == '.json' or path.name.endswith('.data.zip')}

    def replacement(self, *, old_sid='terminal-fixture', sid='replacement-fixture',
                    count=1, finalized=True, ack=True, publish=True):
        rows = [self.row(sid, index=index, count=count, terminal=False) for index in range(count)]
        name = f'campaign_{sid}.json'
        for row in rows:
            row.update(state='done', phase='done', finalized=finalized,
                       campaign_reconciled=ack, evaluation_required=False)
            row['campaign_context'].update(retry_of_session_id=old_sid, history_name=name)
        self.persist(rows)
        if publish:
            self.publish_replacement(rows)
        return rows

    def publish_replacement(self, rows):
        first = rows[0]
        context = first['campaign_context']
        history = {'items': [{'clip_uid': context['clip_uid'], 'task_id': first['task_id'],
            'registry_key': context['registry_key'], 'accounts': [{
                'email': first['account_email'], 'org_key': first['org_key'],
                'session_id': first['session_id'], 'ok': True, 'finalized': True}]}]}
        (config.DATA_DIR / context['history_name']).write_text(json.dumps(history), 'utf8')

    def test_complete_verified_failed_group_releases_owned_video_and_preserves_diagnostics(self):
        rows = [self.row(index=index, count=2) for index in range(2)]
        self.persist(rows)
        before = self.evidence_bytes()
        with patch.object(campaign_evidence, 'publication_index',
                side_effect=AssertionError('Failure is not delivery publication')), \
             patch.object(sent_registry, 'mark_sent_many',
                side_effect=AssertionError('Failure must not be counted as sent')):
            self.assertTrue(upload.all_terminal_remote_failure(rows))
            self.assertEqual(recovery.media_cleanup_protection(), {'paths': set(), 'sha256': set()})
            result = media_lifecycle.cleanup_managed_media([self.video], allowed_roots=(self.media,))
        self.assertEqual(result['files'], 2)
        self.assertFalse(self.video.exists())
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(sent_registry.load(persist_seed=False), {})
        self.assertFalse(list(config.DATA_DIR.glob('campaign_*.json')))

    def test_another_pending_session_protects_shared_video_and_both_journal_groups(self):
        rows = [self.row(), self.row('still-pending', terminal=False)]
        self.persist(rows)
        before = self.evidence_bytes()
        protection = recovery.media_cleanup_protection()
        self.assertIn(self.video, protection['paths'])
        self.assertIn(self.digest, protection['sha256'])
        self.assertNotIn(Path(rows[0]['sidecar_data_path']), protection['paths'])
        result = media_lifecycle.cleanup_managed_media([self.video], allowed_roots=(self.media,))
        self.assertEqual(result['files'], 0)
        self.assertEqual(self.video.read_bytes(), b'Inert verified provider-owned video bytes')
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_pending_hash_reference_keeps_owned_bytes_at_another_path(self):
        terminal = self.row()
        pending = self.row('hash-pending', terminal=False)
        other = self.media / 'another-consumer.mp4'
        other.write_bytes(self.video.read_bytes())
        pending['local_video_path'] = str(other)
        # Legacy lineage may be absent: the persisted transport hash still
        # identifies shared bytes despite a different physical path.
        self.persist([terminal, pending])
        result = media_lifecycle.cleanup_managed_media([self.video], allowed_roots=(self.media,))
        self.assertEqual(result['files'], 0)
        self.assertTrue(self.video.exists())

    def test_unverified_incomplete_and_mixed_groups_retain_all_owned_media(self):
        cases = ('missing-marker', 'missing-part', 'mixed-pending', 'different-context',
                 'wrong-marker-id', 'wrong-marker-owner', 'wrong-marker-session',
                 'wrong-marker-org', 'wrong-marker-task', 'wrong-marker-log',
                 'wrong-marker-date', 'wrong-marker-duration', 'string-marker-version',
                 'bool-marker-version', 'string-marker', 'list-marker', 'contradictory-finalized',
                 'wrong-marker-index', 'wrong-marker-count', 'bool-marker-index', 'bool-marker-count',
                 'float-row-count')
        for fault in cases:
            with self.subTest(fault=fault):
                sid = f'invalid-{fault}'
                rows = [self.row(sid, index=index, count=2) for index in range(2)]
                if fault == 'missing-part':
                    rows.pop()
                elif fault in {'missing-marker', 'mixed-pending'}:
                    rows[1].pop('remote_terminal_failure')
                elif fault == 'different-context':
                    rows[1]['campaign_context']['clip_uid'] = 'another-clip'
                elif fault == 'string-marker':
                    rows[1]['remote_terminal_failure'] = 'failed'
                elif fault == 'list-marker':
                    rows[1]['remote_terminal_failure'] = []
                elif fault == 'contradictory-finalized':
                    rows[1]['finalized'] = True
                elif fault == 'float-row-count':
                    # Python considers 2.0 equal to 2; the journal schema must
                    # still reject a non-integer multipart count.
                    rows[1]['expected_chunk_count'] = 2.0
                else:
                    field, value = {
                        'wrong-marker-id': ('upload_id', 'another-upload'),
                        'wrong-marker-owner': ('email', 'another@example.invalid'),
                        'wrong-marker-session': ('session_id', 'another-session'),
                        'wrong-marker-org': ('org_key', 'another-org'),
                        'wrong-marker-task': ('task_id', 'another-task'),
                        'wrong-marker-log': ('log_id', 'another-log'),
                        'wrong-marker-date': ('recorded_at', '2026-10-08T04:11:30.988Z'),
                        'wrong-marker-duration': ('duration_ms', 1),
                        'string-marker-version': ('version', '1'),
                        'bool-marker-version': ('version', True),
                        'wrong-marker-index': ('chunk_index', 0),
                        'wrong-marker-count': ('expected_chunk_count', 1),
                        'bool-marker-index': ('chunk_index', True),
                        'bool-marker-count': ('expected_chunk_count', True),
                    }[fault]
                    rows[1]['remote_terminal_failure'][field] = value
                self.persist(rows)
                before = self.evidence_bytes()
                self.assertFalse(upload.all_terminal_remote_failure(rows))
                result = media_lifecycle.cleanup_managed_media([self.video], allowed_roots=(self.media,))
                self.assertEqual(result['files'], 0)
                self.assertTrue(self.video.exists())
                self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_source_lineage_hash_of_other_pending_session_remains_protected(self):
        terminal, pending = self.row(), self.row('lineage-pending', terminal=False)
        other = self.media / 'different-video.mp4'
        other.write_bytes(b'Another physical video requiring the same original source')
        pending['local_video_path'] = str(other)
        pending['video_content_sha256'] = hashlib.sha256(other.read_bytes()).hexdigest()
        lineage = {'content': {'assets': {'source_video': {'sha256': self.digest}}}}
        lineage['delivery_binding_sha256'] = content_provenance.canonical_digest(lineage)
        pending['campaign_context']['content_provenance'] = lineage
        self.persist([terminal, pending])
        self.assertIn(self.digest, recovery.media_cleanup_protection()['sha256'])
        result = media_lifecycle.cleanup_managed_media([self.video], allowed_roots=(self.media,))
        self.assertEqual(result['files'], 0)
        self.assertTrue(self.video.exists())

    def test_malformed_pending_transport_hash_stops_cleanup_before_one_deletion(self):
        terminal, pending = self.row(), self.row('bad-pending', terminal=False)
        pending['video_content_sha256'] = 'invalid'
        self.persist([terminal, pending])
        before = self.evidence_bytes()
        with self.assertRaises(ValueError):
            media_lifecycle.cleanup_managed_media([self.video], allowed_roots=(self.media,))
        self.assertTrue(self.video.exists())
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_real_snapshot_keeps_terminal_diagnostics_without_pending_or_sent_credit(self):
        self.persist([self.row(index=index, count=2) for index in range(2)])
        before = self.evidence_bytes()
        with patch.object(upload, 'get_upload', side_effect=AssertionError('Listing is offline')):
            snapshot = recovery.snapshot()
        self.assertEqual(snapshot['pending'], 0)
        self.assertEqual(snapshot['confirmed'], 0)
        self.assertEqual(len(snapshot['items']), 1)
        item = snapshot['items'][0]
        self.assertEqual(item['status'], 'archived_failed')
        self.assertIs(item['terminal_remote_failure'], True)
        self.assertIs(item['can_resume'], False)
        self.assertEqual(recovery.campaign_exclusions(snapshot['items']), {})
        self.assertEqual(sent_registry.load(persist_seed=False), {})
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_real_snapshot_of_incomplete_terminal_group_keeps_clip_reserved(self):
        self.persist([self.row(count=2)])
        snapshot = recovery.snapshot()
        self.assertEqual(snapshot['pending'], 1)
        self.assertEqual(snapshot['confirmed'], 0)
        item = snapshot['items'][0]
        self.assertEqual(item['status'], 'needs_review')
        self.assertIs(item['terminal_remote_failure'], False)
        self.assertEqual(recovery.campaign_exclusions(snapshot['items']), {
            'fixture-clip': ['fixture@example.invalid']})
        self.assertIn(self.video, recovery.media_cleanup_protection()['paths'])

    def test_failed_complete_snapshot_offers_remote_inspection_without_reading_mp4_or_zip(self):
        row = self.row(terminal=False)
        self.persist([row])
        protected_payloads = {self.video, Path(row['sidecar_data_path'])}
        original_open = Path.open

        def metadata_only_open(path, *args, **kwargs):
            if path in protected_payloads:
                raise AssertionError('Enumeration must not open persisted MP4/ZIP bytes')
            return original_open(path, *args, **kwargs)

        with patch.object(Path, 'open', metadata_only_open), \
             patch.object(upload, '_video_resume_payload',
                side_effect=AssertionError('Enumeration must not validate video transport bytes')), \
             patch.object(upload, 'get_upload', side_effect=AssertionError('Listing is offline')):
            snapshot = recovery.snapshot()
        self.assertFalse(upload.is_pending_transport(row))
        self.assertEqual(snapshot['pending'], 1)
        self.assertEqual(snapshot['confirmed'], 0)
        self.assertIs(snapshot['items'][0]['can_resume'], True)
        self.assertIs(snapshot['items'][0]['terminal_remote_failure'], False)
        self.assertEqual(snapshot['items'][0]['status'], 'pending')
        self.assertIn('fixture-clip', recovery.campaign_exclusions(snapshot['items']))

    def test_real_snapshot_other_account_pending_remains_reserved_after_terminal_archival(self):
        terminal = self.row()
        pending = self.row('another-owner-pending', terminal=False)
        pending['account_email'] = 'another@example.invalid'
        self.persist([terminal, pending])
        snapshot = recovery.snapshot()
        self.assertEqual(snapshot['pending'], 1)
        self.assertEqual(snapshot['confirmed'], 0)
        self.assertEqual(recovery.campaign_exclusions(snapshot['items']), {
            'fixture-clip': ['another@example.invalid']})
        result = media_lifecycle.cleanup_managed_media([self.video], allowed_roots=(self.media,))
        self.assertEqual(result['files'], 0)
        self.assertTrue(self.video.exists())

    def test_old_archive_is_removed_only_after_linked_delivery_ack_and_publication(self):
        old = self.row()
        self.persist([old])
        original_json = (self.journals / upload._sidecar_filename(old['session_id'])).read_bytes()
        archive = Path(old['sidecar_data_path'])
        original_zip = archive.read_bytes()
        self.assertEqual(recovery.release_replaced_terminal_archives()['archives_removed'], 0)
        new = self.replacement(finalized=False, ack=False, publish=False)
        self.assertEqual(recovery.release_replaced_terminal_archives()['archives_removed'], 0)
        new[0]['finalized'] = True
        self.persist(new)
        self.assertEqual(recovery.release_replaced_terminal_archives()['archives_removed'], 0)
        self.publish_replacement(new)
        self.assertEqual(recovery.release_replaced_terminal_archives()['archives_removed'], 0)
        self.assertEqual(archive.read_bytes(), original_zip)
        report = recovery.reconcile_confirmed(refresh=False)
        self.assertEqual(report['reconciled'], 1)
        self.assertEqual(report['archives_removed'], 1)
        self.assertEqual(report['archive_bytes_removed'], len(original_zip))
        self.assertEqual(report['archive_cleanup_errors'], [])
        self.assertFalse(archive.exists())
        self.assertEqual((self.journals / upload._sidecar_filename(old['session_id'])).read_bytes(), original_json)
        self.assertTrue(Path(new[0]['sidecar_data_path']).exists())
        self.assertEqual(recovery.release_replaced_terminal_archives()['archives_removed'], 0)

    def test_multipart_terminal_archives_keep_json_after_full_replacement_publication(self):
        old = [self.row(index=index, count=2) for index in range(2)]
        self.persist(old)
        new = self.replacement(count=2)
        old_json = {self.journals / upload._sidecar_filename(row['session_id'], row['chunk_index']): None
                    for row in old}
        old_json = {path: path.read_bytes() for path in old_json}
        expected_bytes = sum(Path(row['sidecar_data_path']).stat().st_size for row in old)
        report = recovery.release_replaced_terminal_archives()
        self.assertEqual(report['archives_removed'], 2)
        self.assertEqual(report['archive_bytes_removed'], expected_bytes)
        self.assertEqual({path: path.read_bytes() for path in old_json}, old_json)
        self.assertTrue(all(Path(row['sidecar_data_path']).exists() for row in new))

    def test_pending_path_or_hash_reference_preserves_terminal_archive(self):
        for guard in ('path', 'hash'):
            with self.subTest(guard=guard):
                sid = f'old-archive-{guard}'
                old = self.row(sid)
                self.persist([old])
                self.replacement(old_sid=sid, sid=f'new-archive-{guard}')
                pending = self.row(f'pending-archive-{guard}', terminal=False)
                if guard == 'path':
                    pending['sidecar_data_path'] = old['sidecar_data_path']
                    pending.pop('sidecar_sha256')
                else:
                    pending['sidecar_sha256'] = old['sidecar_sha256']
                self.persist([pending])
                before = Path(old['sidecar_data_path']).read_bytes()
                report = recovery.release_replaced_terminal_archives()
                self.assertEqual(report['archives_removed'], 0)
                self.assertGreaterEqual(report['archives_retained'], 1)
                self.assertEqual(Path(old['sidecar_data_path']).read_bytes(), before)

    def test_corrupt_archive_is_reported_and_preserved_without_reversing_confirmed_delivery(self):
        old = self.row()
        self.persist([old])
        self.replacement()
        archive = Path(old['sidecar_data_path'])
        corrupted = b'Corrupt bytes with the same size as the original ZIP'
        corrupted = (corrupted + b'!' * old['sidecar_size_bytes'])[:old['sidecar_size_bytes']]
        archive.write_bytes(corrupted)
        report = recovery.reconcile_confirmed(refresh=False)
        self.assertEqual(report['archives_removed'], 0)
        self.assertEqual(report['archive_cleanup_errors'][0]['code'], 'terminal_archive_preserved')
        self.assertEqual(archive.read_bytes(), corrupted)
        self.assertIn('fixture@example.invalid', sent_registry.sent_emails('minute|fixture-task', 'fixture-clip'))
        self.assertTrue(upload.load_sidecar('replacement-fixture')['campaign_reconciled'])

    def test_archive_unlink_failure_is_visible_and_preserves_delivery_and_receipts(self):
        old = self.row()
        self.persist([old])
        self.replacement()
        before = self.evidence_bytes()
        with patch.object(upload, '_remove_sidecar_archive', return_value=None):
            report = recovery.reconcile_confirmed(refresh=False)
        self.assertEqual(report['archives_removed'], 0)
        self.assertEqual(report['archives_retained'], 1)
        self.assertEqual(report['archive_cleanup_errors'][0]['code'], 'terminal_archive_preserved')
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_changed_old_journal_during_hash_prevents_archive_removal(self):
        old = self.row()
        self.persist([old])
        self.replacement()
        path = self.journals / upload._sidecar_filename(old['session_id'])
        original = upload._read_sidecar_file
        reads = 0

        def changed_on_recheck(current_path):
            nonlocal reads
            row = original(current_path)
            if current_path == path:
                reads += 1
                if reads == 3:
                    row = {**row, 'error': 'Updated diagnostic from another writer'}
                    path.write_text(json.dumps(row), 'utf8')
            return row

        with patch.object(upload, '_read_sidecar_file', side_effect=changed_on_recheck):
            report = recovery.release_replaced_terminal_archives()
        self.assertEqual(report['archives_removed'], 0)
        self.assertTrue(report['archive_cleanup_errors'])
        self.assertTrue(Path(old['sidecar_data_path']).exists())

    def test_unlinked_partial_or_wrong_identity_replacement_never_releases_archive(self):
        cases = ('unlinked', 'different-owner', 'different-task', 'different-clip', 'different-duration',
                 'missing-part', 'unpublished', 'missing-ack')
        for fault in cases:
            with self.subTest(fault=fault):
                sid = f'preserved-old-{fault}'
                old = self.row(sid)
                self.persist([old])
                new = [self.row(f'replacement-{fault}', terminal=False)]
                new[0].update(state='done', phase='done', finalized=True,
                    campaign_reconciled=fault != 'missing-ack', evaluation_required=False)
                new[0]['campaign_context'].update(retry_of_session_id=sid,
                    history_name=f'campaign_replacement-{fault}.json')
                if fault == 'unlinked':
                    new[0]['campaign_context'].pop('retry_of_session_id')
                elif fault == 'different-owner':
                    new[0]['account_email'] = 'another@example.invalid'
                elif fault == 'different-task':
                    new[0]['task_id'] = new[0]['campaign_context']['task_id'] = 'another-task'
                elif fault == 'different-clip':
                    new[0]['campaign_context']['clip_uid'] = 'another-clip'
                elif fault == 'different-duration':
                    new[0]['duration_ms'] -= 1
                elif fault == 'missing-part':
                    new[0]['expected_chunk_count'] = 2
                self.persist(new)
                if fault != 'unpublished':
                    self.publish_replacement(new)
                report = recovery.release_replaced_terminal_archives()
                self.assertEqual(report['archives_removed'], 0)
                self.assertTrue(Path(old['sidecar_data_path']).exists())


class TerminalFailedExclusionTests(unittest.TestCase):
    def item(self, **changes):
        return {'email': 'fixture@example.invalid', 'clip_uid': 'fixture-clip',
            'delivery_clip_uids': ['fixture-clip', 'nymeria-planned:fixture'],
            'terminal_remote_failure': True, 'chunks_found': 2, 'chunks_expected': 2,
            'can_resume': False, **changes}

    def test_verified_terminal_group_releases_canonical_and_acquisition_alias(self):
        self.assertEqual(recovery.campaign_exclusions([self.item()]), {})

    def test_other_pending_session_retains_reservation_for_same_account_and_clip(self):
        terminal = self.item()
        pending = self.item(terminal_remote_failure=False, can_resume=True)
        self.assertEqual(recovery.campaign_exclusions([terminal, pending]), {
            'fixture-clip': ['fixture@example.invalid'],
            'nymeria-planned:fixture': ['fixture@example.invalid']})

    def test_terminal_marker_does_not_release_another_accounts_pending_pair(self):
        pending = self.item(email='other@example.invalid', terminal_remote_failure=False)
        self.assertEqual(recovery.campaign_exclusions([self.item(), pending]), {
            'fixture-clip': ['other@example.invalid'],
            'nymeria-planned:fixture': ['other@example.invalid']})

    def test_incomplete_resumable_or_untyped_terminal_flag_never_releases_reservation(self):
        for changes in ({'chunks_found': 1}, {'chunks_expected': 0}, {'chunks_expected': '2'},
                        {'chunks_found': True}, {'can_resume': True}, {'can_resume': None},
                        {'terminal_remote_failure': 'true'}, {'terminal_remote_failure': 1},
                        {'terminal_remote_failure': False}):
            with self.subTest(changes=changes):
                self.assertIn('fixture-clip', recovery.campaign_exclusions([self.item(**changes)]))


if __name__ == '__main__':
    unittest.main()
