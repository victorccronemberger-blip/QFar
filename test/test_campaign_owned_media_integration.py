"""Offline scheduler with real media ownership, journals, and receipt publication."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from moneymin import campaign, config, recovery, upload
from moneymin.campaign_evidence import publication_index, publication_registered
from moneymin.campaign_types import AccountSpec, CampaignConfig, TaskSpec
from moneymin.media_lifecycle import record_managed_media
from moneymin.upload_types import journal_delivery_confirmed


class OwnedMediaCampaignIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='qmoney-physical-stream-'))
        self.root = Path(folder).resolve()
        self.media = self.root / 'media/ego4d'
        self.media.mkdir(parents=True)
        self.stack.enter_context(patch.multiple(config, DATA_DIR=self.root / 'state',
                                              MEDIA_DATA_DIR=self.root / 'media', SECRETS_DIR=self.root / 'secrets'))
        self.stack.enter_context(patch.object(campaign.holoassist, 'data_dir', return_value=self.root / 'holoassist'))
        self.accounts = [AccountSpec(email, 'offline-org') for email in
                         ('first@example.invalid', 'second@example.invalid')]
        self.task = TaskSpec('offline-task', 'offline-scenario', 300, 1800, count=2)
        self.clips = [{'clip_uid': uid, 'parent_video_uid': uid, 'dur_s': 300, 'source': 'ego4d'}
                      for uid in ('one', 'two')]
        self.cfg = CampaignConfig(self.accounts, [self.task], work_dir=self.media,
            candidate_plan={self.task.task_id: self.clips}, account_workers=2,
            account_gap_s=0, shuffle_schedule=False, cleanup_after_upload=True)
        self.events = []
        self.created = {}
        self.fail_account = None
        self.stack.enter_context(patch.object(campaign, '_clip_is_cached', return_value=False))
        self.stack.enter_context(patch.object(campaign, '_ego_clip_inputs', side_effect=lambda clip: (clip, {})))
        self.prepare = self.stack.enter_context(patch.object(campaign, 'prepare_clip', side_effect=self.prepare_item))
        self.stack.enter_context(patch.object(campaign, 'upload_to_account', side_effect=self.deliver))
        self.prefetch = self.stack.enter_context(patch.object(campaign, '_prefetch_following'))
        self.warm = self.stack.enter_context(patch.object(campaign, '_warm_account_videos'))
        self.manual = self.media / 'manual_recording.mp4'
        self.manual.write_bytes(b'owner supplied recording; never managed')

    def prepare_item(self, clip, *args, **kwargs):
        uid = clip['clip_uid']
        if uid == 'two':
            self.assertTrue(self.created['one'])
            self.assertFalse(any(path.exists() for path in self.created['one']),
                             'The next dataset download began while first-item bytes remained')
            first = [rows for rows in recovery._groups(include_reconciled=True)
                     if rows[0]['campaign_context']['clip_uid'] == 'one']
            self.assertEqual(len(first), len(self.cfg.accounts))
            self.assertTrue(all(publication_registered(rows, publication_index()) for rows in first))
        self.events.append(('acquire', uid))
        paths = [self.media / (uid + suffix) for suffix in ('.mp4', '_native.mp4', '_imu.csv')]
        for index, path in enumerate(paths):
            payload = f'declared inert physical {uid} provider asset {index}'.encode()
            path.write_bytes(payload)
            record_managed_media(path, root=self.media, provider='ego4d', role='offline_fixture_source',
                                 expected_digest=hashlib.sha256(payload).hexdigest())
        native_marker = paths[1].with_name(paths[1].name + '.source.json')
        native_marker.write_text(json.dumps({'version': campaign._NATIVE_CACHE_VERSION,
            'source_sha256': hashlib.sha256(paths[0].read_bytes()).hexdigest(),
            'prepared_sha256': hashlib.sha256(paths[1].read_bytes()).hexdigest(),
            'prepared_size': paths[1].stat().st_size}), 'utf8')
        self.created[uid] = [*paths, *(path.with_name(path.name + '.managed.json') for path in paths), native_marker]
        return {'duration_ms': 300000, 'video_path': str(paths[1]), 'imu_real': True,
                'source': 'ego4d', '_cleanup_paths': [str(path) for path in paths]}

    def deliver(self, item, account, *args, **kwargs):
        uid = item['clip_uid']
        sid = uid + '-' + account.email.split('@')[0] + '-offline-session'
        failed = account.email == self.fail_account
        upload.save_sidecar({'session_id': sid, 'account_email': account.email,
            'org_key': account.org_key, 'task_id': item['task_id'], 'chunk_index': 0,
            'expected_chunk_count': 1, 'state': 'failed' if failed else 'done',
            'phase': 'complete' if failed else 'done', 'create_attempted': True,
            'upload_id': sid + '-receipt', 'finalized': not failed,
            'evaluation_required': False, 'video_path': item['video_path'],
            'campaign_context': {'registry_key': item['registry_key'], 'clip_uid': uid,
                                 'task_id': item['task_id']}})
        self.events.append(('receipt', uid, account.email))
        return {'email': account.email, 'org_key': account.org_key, 'ok': not failed,
                'finalized': not failed, 'session_id': sid,
                **({'error': 'declared inert uncertain completion'} if failed else {})}

    def test_one_account_releases_both_actual_items_before_next_source_acquisition(self):
        self.cfg.accounts = self.accounts[:1]
        log = campaign.run_campaign(self.cfg)
        self.assertEqual(log.status, 'done')
        self.assertEqual([row for row in self.events if row[0] == 'acquire'], [('acquire', 'one'), ('acquire', 'two')])
        self.assertFalse(any(path.exists() for values in self.created.values() for path in values))
        groups = recovery._groups(include_reconciled=True)
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(journal_delivery_confirmed(row) and row.get('campaign_reconciled') is True
                            for rows in groups for row in rows))
        self.assertTrue(all(publication_registered(rows, publication_index()) for rows in groups))
        self.assertEqual(self.manual.read_bytes(), b'owner supplied recording; never managed')
        self.prefetch.assert_not_called()
        self.warm.assert_not_called()

    def test_two_account_batch_publishes_all_receipts_and_releases_real_files_before_next_source(self):
        log = campaign.run_campaign(self.cfg)
        self.assertEqual(log.status, 'done')
        self.assertEqual(self.prepare.call_count, 2)
        self.assertFalse(any(path.exists() for values in self.created.values() for path in values))
        groups = recovery._groups(include_reconciled=True)
        self.assertEqual(len(groups), 4)
        self.assertTrue(all(publication_registered(rows, publication_index()) for rows in groups))
        self.assertTrue(self.manual.exists())
        self.prefetch.assert_not_called()
        self.warm.assert_not_called()

    def test_actual_pending_receipt_retains_first_source_and_never_acquires_second(self):
        self.fail_account = self.accounts[0].email
        with self.assertRaisesRegex(RuntimeError, 'não adquiriu outro vídeo'):
            campaign.run_campaign(self.cfg)
        self.assertEqual(self.prepare.call_count, 1)
        self.assertTrue(all(path.exists() for path in self.created['one']))
        pending = next(rows for rows in recovery._groups(include_reconciled=True)
                       if rows[0]['account_email'] == self.fail_account)
        self.assertFalse(journal_delivery_confirmed(pending[0]))
        self.assertIn(Path(pending[0]['video_path']), recovery.media_cleanup_protection()['paths'])
        self.assertTrue(self.manual.exists())

    def test_confirmed_account_keeps_shared_variant_chunks_until_other_account_publishes(self):
        self.cfg.unique_video = True
        self.task.count = 1
        self.cfg.candidate_plan[self.task.task_id] = self.clips[:1]
        built, allow_second_journal = threading.Event(), threading.Event()
        shared_profile = SimpleNamespace(device_id='01234567-shared-fixture')
        family = []
        observed = []
        original_cleanup = campaign._cleanup_confirmed_account_media

        def deliver_shared(item, account, *args, **kwargs):
            variant = campaign._account_video_path(Path(item['video_path']), shared_profile)
            if account is self.accounts[1]:
                # This worker has built its cut, but has not yet published a
                # journal. The sibling's confirmation cannot unlink this cut.
                payload = ('declared shared fixture ' + variant.name).encode()
                variant.write_bytes(payload)
                record_managed_media(variant, root=self.media, provider='ego4d', role='account_video',
                    expected_digest=hashlib.sha256(payload).hexdigest())
                chunk = campaign._chunk_video_path(variant, 0, 0, 300000)
                payload = ('declared shared fixture ' + chunk.name).encode()
                chunk.write_bytes(payload)
                record_managed_media(chunk, root=self.media, provider='ego4d', role='account_video',
                    expected_digest=hashlib.sha256(payload).hexdigest())
                family.extend((variant, chunk))
                built.set()
                if not allow_second_journal.wait(5):
                    raise RuntimeError('Second-account fixture timed out before publication')
                self.assertTrue(all(path.is_file() for path in family))
                return self.deliver({**item, 'video_path': str(chunk)}, account)
            if not built.wait(5):
                raise RuntimeError('Shared variant fixture was not produced')
            chunk = campaign._chunk_video_path(variant, 0, 0, 300000)
            result = self.deliver({**item, 'video_path': str(chunk)}, account)
            result['_delivery_media_paths'] = [str(variant), str(chunk)]
            return result

        def cleanup_observer(item, account, result, paths, work_dir, **kwargs):
            released = original_cleanup(item, account, result, paths, work_dir, **kwargs)
            if account is self.accounts[0]:
                self.assertTrue(all(path.is_file() for path in family))
                self.assertFalse(upload._sidecar_path('one-second-offline-session', 0).exists())
                self.assertIsNone(released)
                observed.append('first_receipt_published_shared_family_preserved')
                allow_second_journal.set()
            return released

        with patch.object(campaign, 'upload_to_account', side_effect=deliver_shared), \
                patch.object(campaign.device_profile, 'get_profile', return_value=shared_profile), \
                patch.object(campaign, '_cleanup_confirmed_account_media', side_effect=cleanup_observer):
            try:
                log = campaign.run_campaign(self.cfg)
            finally:
                allow_second_journal.set()
        self.assertEqual(log.status, 'done')
        self.assertEqual(observed, ['first_receipt_published_shared_family_preserved'])
        self.assertFalse(any(path.exists() for path in family))
        self.assertEqual(len(recovery._groups(include_reconciled=True)), 2)


if __name__ == '__main__':
    unittest.main()
