"""Exact acquisition aliases share one receipt and one explicit reset scope."""
from contextlib import ExitStack
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from moneymin import config, sent_registry, upload
from moneymin.content_provenance import canonical_digest


class NymeriaReceiptAliasTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='qmoney-nymeria-alias-'))
        self.root = Path(folder).resolve()
        self.stack.enter_context(patch.multiple(config, DATA_DIR=self.root, MEDIA_DATA_DIR=self.root,
                                              SECRETS_DIR=self.root / 'secrets'))
        self.key = 'minute|fixture-task|Task'
        self.email = 'owner@example.invalid'
        self.canonical = 'nymeria:sequence:10.000:460.000'
        self.planned = 'nymeria-planned:sequence:1010000000000:1460000000000'

    def item(self, *, sid='alias-session', confirmed=True):
        task = {'name': 'Fixture task', 'id': 'fixture-task', 'registry_key': self.key}
        absolute = [1010000000000, 1460000000000]
        plan = {'clip_uid': self.planned, 'exported_clip_uid': self.planned, 'seq_id': 'sequence',
            'parent_video_uid': 'sequence', 'source': 'nymeria', 'acquisition_required': True,
            'source_clock_domain': 'aria_DEVICE_TIME_ns', 'planned_device_window_ns': absolute,
            'device_window_ns': absolute, 'selection_evidence': {'schema': 1, 'dataset': 'nymeria',
                'algorithm': 'nymeria-atomic-device-v4-planned', 'task': task,
                'planned_device_window_ns': absolute, 'sensor_coverage': 'unmeasured'}}
        evidence = {'schema': 1, 'dataset': 'nymeria', 'algorithm': 'nymeria-atomic-device-v4',
            'task': task, 'window_s': [10.0, 460.0], 'measured_window_ns': [1000000000000, 1470000000000],
            'acquisition_plan': plan}
        lineage = {'schema': 1, 'dataset': 'nymeria', 'clip_uid': self.canonical,
            'parent_video_uid': 'sequence', 'window_s': [10.0, 460.0], 'selection_evidence': evidence,
            'assets': {'prepared_video': {'name': 'declared fixture.mp4', 'bytes': 10, 'sha256': 'a' * 64}}}
        lineage['lineage_sha256'] = canonical_digest(lineage)
        return {'clip_uid': self.canonical, 'task_id': 'fixture-task', 'registry_key': self.key,
            'content_provenance': lineage, 'dedup_clip_uids': [self.planned, 'arbitrary-other-uid'],
            'accounts': [{'email': self.email, 'org_key': 'fixture-org', 'session_id': sid,
                          'ok': confirmed, 'finalized': confirmed}]}

    def history(self, name, item):
        path = self.root / name
        path.write_text(json.dumps({'items': [item]}), 'utf8')
        return path

    def journal(self, *, sid='alias-session', finalized=True, name='campaign_old.json'):
        return upload.save_sidecar({'session_id': sid, 'account_email': self.email, 'org_key': 'fixture-org',
            'task_id': 'fixture-task', 'chunk_index': 0, 'expected_chunk_count': 1,
            'upload_id': 'fixture-receipt-' + sid, 'state': 'done' if finalized else 'transport',
            'finalized': finalized, 'campaign_context': {'registry_key': self.key,
                'clip_uid': self.canonical, 'task_id': 'fixture-task', 'history_name': name}})

    def mark(self, item):
        sent_registry.mark_sent_many([(self.key, uid, self.email) for uid in sent_registry.delivery_clip_uids(item)])

    def test_exact_lineage_alias_is_indexed_but_never_counts_as_second_delivery(self):
        item = self.item()
        self.assertEqual(sent_registry.delivery_clip_uids(item), [self.canonical, self.planned])
        self.mark(item)
        self.assertEqual(sent_registry.sent_emails(self.key, self.planned), {self.email})
        self.assertNotIn('arbitrary-other-uid', sent_registry.all_uids())
        self.assertEqual(sent_registry.summary()[0]['sent_clips'], 1)
        self.assertEqual(sent_registry.summary()[0]['sends'], 1)

    def test_history_seed_reconstructs_same_alias_without_any_source_files(self):
        self.history('campaign_old.json', self.item())
        self.assertEqual(sent_registry.load(), {self.key: {self.canonical: [self.email], self.planned: [self.email]}})
        self.assertFalse((self.root / 'sequence').exists())

    def test_scope_reset_clears_both_ids_preserves_canonical_journal_and_does_not_reseed(self):
        item = self.item()
        history = self.history('campaign_old.json', item)
        journal = self.journal()
        before = {p: p.read_bytes() for p in (history, journal)}
        self.mark(item)
        sent_registry.reset(history_names=['campaign_old.json'])
        self.assertEqual(sent_registry.load(), {})
        self.assertEqual(sent_registry._reset_history()['completed_sessions'], ['alias-session'])
        (self.root / sent_registry.FILE_NAME).unlink()
        self.assertEqual(sent_registry.load(), {})
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertEqual(upload.load_sidecar('alias-session')['campaign_context']['clip_uid'], self.canonical)

    def test_scope_reset_keeps_alias_needed_by_newer_receipt(self):
        old = self.item(sid='old')
        self.history('campaign_old.json', old)
        self.journal(sid='old')
        self.history('campaign_current.json', self.item(sid='current'))
        self.journal(sid='current', name='campaign_current.json')
        self.mark(old)
        sent_registry.reset(history_names=['campaign_old.json'])
        self.assertEqual(sent_registry.sent_emails(self.key, self.planned), {self.email})
        self.assertEqual(sent_registry.sent_emails(self.key, self.canonical), {self.email})

    def test_pending_group_keeps_alias_and_its_canonical_receipt_during_scope_reset(self):
        old = self.item(sid='pending')
        self.history('campaign_old.json', old)
        self.journal(sid='pending', finalized=False)
        self.mark(old)
        sent_registry.reset(history_names=['campaign_old.json'])
        self.assertEqual(sent_registry.sent_emails(self.key, self.planned), {self.email})
        self.assertEqual(sent_registry.sent_emails(self.key, self.canonical), {self.email})
        self.assertEqual(sent_registry._reset_history()['completed_sessions'], [])

    def test_invalid_forged_plan_bounds_or_lineage_cannot_adopt_alias(self):
        for kind in ('digest', 'alias', 'window', 'task'):
            with self.subTest(kind=kind):
                item = self.item()
                lineage = item['content_provenance']
                if kind == 'digest':
                    lineage['lineage_sha256'] = '0' * 64
                elif kind == 'alias':
                    lineage['selection_evidence']['acquisition_plan']['clip_uid'] = 'nymeria-planned:other:1:2'
                elif kind == 'window':
                    lineage['window_s'] = [20, 470]
                else:
                    item['task_id'] = 'foreign-task'
                if kind != 'digest':
                    lineage['lineage_sha256'] = canonical_digest({k: v for k, v in lineage.items() if k != 'lineage_sha256'})
                with self.assertRaises(ValueError):
                    sent_registry.delivery_clip_uids(item)

    def test_delivery_bound_context_alias_requires_both_publication_hashes(self):
        item = self.item()
        delivery = {'schema': 1, 'content': item['content_provenance'], 'chunks': [],
                    'task_id': 'fixture-task', 'org_key': 'fixture-org', 'session_id': 'alias-session'}
        delivery['delivery_binding_sha256'] = canonical_digest(delivery)
        item['content_provenance'] = delivery
        self.assertEqual(sent_registry.delivery_clip_uids(item), [self.canonical, self.planned])
        delivery['delivery_binding_sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            sent_registry.delivery_clip_uids(item)

    def test_bound_delivery_cannot_replay_alias_with_foreign_session_or_org(self):
        for field in ('session_id', 'org_key'):
            with self.subTest(field=field):
                item = self.item()
                envelope = {'schema': 1, 'content': item['content_provenance'], 'chunks': [],
                    'task_id': 'fixture-task', 'org_key': 'fixture-org', 'session_id': 'alias-session'}
                envelope['delivery_binding_sha256'] = canonical_digest(envelope)
                item['content_provenance'] = envelope
                item[field] = 'foreign-identity'
                with self.assertRaises(ValueError):
                    sent_registry.delivery_clip_uids(item)

    def test_invalid_stored_alias_proof_blocks_reset_before_registry_or_history_mutation(self):
        item = self.item()
        self.mark(item)
        item['content_provenance']['lineage_sha256'] = '0' * 64
        history = self.history('campaign_old.json', item)
        before = {p: p.read_bytes() for p in (history, self.root / sent_registry.FILE_NAME)}
        with self.assertRaises(ValueError):
            sent_registry.reset(history_names=['campaign_old.json'])
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertFalse((self.root / 'sent_reset_history.json').exists())


if __name__ == '__main__':
    unittest.main()
