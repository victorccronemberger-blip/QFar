"""Real local campaign chain with inert providers; immutable attempts/receipts."""
from dataclasses import replace
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from moneymin import campaign, campaign_types, config, device_profile, recovery, sent_registry, upload
from moneymin.web import runner, server
import test_campaign_end_to_end as fixtures


class CampaignPublicationIntegrityTests(unittest.TestCase):
    def setUp(self):
        real_mark, real_sent, real_all = sent_registry.mark_sent, sent_registry.sent_emails, sent_registry.is_sent_to_all
        fixture = fixtures.CampaignEndToEndTests('test_success_matches_polling_and_persisted_history')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.email = fixture.emails[0]
        self.sid = 'publication-contract'
        self.wire = []
        self.clock = device_profile.format_recorded_at(time.time() - 300)
        fixture.body = {**fixture.body, 'accounts': [self.email]}
        (fixture.root / 'journals').mkdir()
        (fixture.root / 'fixture.mp4').write_bytes(b'Declared inert media, no capture evidence.')
        fixture.prepare.side_effect = lambda *a, **k: {
            'duration_ms': 60_000, 'video_path': str(fixture.root / 'fixture.mp4'),
            'imu_real': True, 'imu_csv': 'opaque inert IMU'}
        fixture.send.side_effect = fixtures.REAL_UPLOAD_TO_ACCOUNT
        fixture.mark.side_effect = real_mark
        outer = self
        class Session:
            _live = True
            recording_policy = None
            _moneymin_pending_pumped = False
            def __init__(self, email): self.email = email
            def ensure_auth(self, **kwargs): return {'email': self.email}
            def warmup(self): pass
            def all_tasks(self, org_key=None):
                return [{'id': 'task', 'name': 'Furniture Assembly', 'scenario': 'assembling furniture'}]
            def request(self, method, path, body=None):
                outer.wire.append((method, path, body))
                if path.startswith('/api/v1/uploads?'): return 201, json.dumps({'id': 'publication-upload', 'status': 'initiated', 'meta': {}})
                if path == '/api/v1/storage/sas/blobs':
                    return 200, json.dumps({'signed_urls': [
                        {'filename': row['filename'], 'blob_url': 'https://blob.invalid/' + row['filename'], 'expires_at': '2030-01-01T00:00:00Z'}
                        for row in body['files']]})
                if path.endswith('/complete'): return 200, json.dumps({'id': path.split('/')[-2], 'status': 'uploaded', 'meta': {}})
                if path.endswith('/evaluate'):
                    return 200, json.dumps({'upload_id': 'publication-upload', 'checks': [{'id': 'inert', 'label': 'Declared inert quality', 'status': 'pass', 'detail': None}]})
                if path.endswith('/finalize'): return 204, ''
                raise AssertionError('Undeclared provider fixture operation')
        probe = {'duration_ms': 60_000, 'fps': 30, 'has_video': True, 'has_audio': True,
                 'width': 1440, 'height': 1080, 'codec': 'h264'}
        contexts = (
            patch.object(server.Session, 'from_email', side_effect=lambda email, **kw: Session(email)),
            patch.object(runner, 'run_campaign', side_effect=lambda cfg, **kw: campaign.run_campaign(
                replace(cfg, realistic_timeline=False, allow_new_accounts=True, shuffle_schedule=False,
                        account_workers=1, account_max_attempts=3, account_retry_s=0), **kw)),
            patch.object(campaign.org_policy, 'account_kind', return_value='claru'),
            patch.object(campaign.device_profile, 'get_profile', side_effect=lambda email: SimpleNamespace(
                email=email, frames_gop=30, uptime_ns_at=lambda wall: 1,
                upload_device_meta=lambda: {'declared': 'inert'}, upload_platform_meta=lambda: {'declared': 'inert'})),
            patch.object(campaign, '_new_identity', side_effect=lambda *a, **kw: (self.sid, self.sid + '_0', self.clock)),
            patch.object(campaign, '_chunk_plan', return_value=[(0, 60_000)]),
            patch.object(campaign, 'build_frames_csv_from_video', return_value='opaque inert frames'),
            patch.object(campaign, '_build_sidecar', return_value=b'Declared inert ZIP bytes'),
            patch.object(campaign, 'probe_video', return_value=probe),
            patch.object(upload, 'probe_video', return_value=probe),
            patch.object(upload, '_probe_duration_ms', return_value=60_000),
            patch.object(upload, '_validate_sidecar_zip', return_value={'declared': 'inert'}),
            patch('moneymin.validate.validate_upload_meta', return_value=[]),
            patch.object(upload, '_put_blob_file', return_value=201),
            patch.object(upload, '_put_blob', return_value=201),
            patch.object(upload, 'sidecars_dir', return_value=fixture.root / 'journals'),
            patch.object(campaign.sent_registry, 'sent_emails', side_effect=real_sent),
            patch.object(campaign.sent_registry, 'is_sent_to_all', side_effect=real_all),
        )
        for context in contexts: fixture.stack.enter_context(context)

    def run_campaign(self):
        response = self.fixture.client.post('/api/campaigns', json=self.fixture.body)
        self.assertEqual(response.status_code, 200, response.get_json())
        return self.fixture.finish()

    def test_observer_failure_before_success_event_restores_confirmed_polling_once(self):
        real_event = self.fixture.instance._on_event
        def event(kind, payload):
            if kind == 'account_done': raise RuntimeError('Declared observer failure')
            return real_event(kind, payload)
        with patch.object(self.fixture.instance, '_on_event', side_effect=event):
            snapshot, history = self.run_campaign()
        self.assertEqual(snapshot['state'], 'error')
        self.assertEqual(history['status'], 'error')
        self.assertEqual(snapshot['totals']['ok_sends'], 1)
        self.assertEqual(snapshot['totals']['done_sends'], 1)
        self.assertEqual(snapshot['log_path'], next(self.fixture.root.glob('campaign_*.json')).name)
        self.assertFalse(any(row['kind'] == 'account_done' for row in snapshot['events']))
        self.assertIs(upload.load_sidecar(self.sid, 0)['finalized'], True)

    def test_observer_failure_after_success_event_does_not_double_credit(self):
        real_event = self.fixture.instance._on_event
        def event(kind, payload):
            result = real_event(kind, payload)
            if kind == 'account_done': raise RuntimeError('Declared observer failure after update')
            return result
        with patch.object(self.fixture.instance, '_on_event', side_effect=event):
            snapshot, _history = self.run_campaign()
        self.assertEqual(snapshot['state'], 'error')
        self.assertEqual(snapshot['totals']['ok_sends'], 1)
        self.assertEqual(snapshot['totals']['done_sends'], 1)

    def test_history_failure_keeps_confirmed_receipt_visible_after_repeated_reconciliation(self):
        real_save = campaign_types.CampaignLog.save
        def save(log, path=None):
            if log.items: raise OSError('Declared persistent history failure')
            return real_save(log, path)
        with patch.object(campaign_types.CampaignLog, 'save', save):
            snapshot, history = self.run_campaign()
        self.assertEqual(snapshot['state'], 'error')
        self.assertEqual(history['items'], [])
        path = next(self.fixture.root.glob('campaign_*.json'))
        original = path.read_bytes()
        operations = list(self.wire)
        for _attempt in range(2):
            response = self.fixture.client.post('/api/recovery/reconcile')
            self.assertEqual(response.status_code, 200, response.get_json())
            items = self.fixture.client.get('/api/recovery').get_json()['items']
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]['session_id'], self.sid)
            self.assertEqual(items[0]['status'], 'confirmed')
            self.assertIs(items[0]['publication_pending'], True)
            self.assertIs(items[0]['can_resume'], False)
        self.assertEqual(self.wire, operations)
        self.assertEqual(path.read_bytes(), original)
        key = upload.load_sidecar(self.sid, 0)['campaign_context']['registry_key']
        self.assertEqual(sent_registry.sent_emails(key, 'clip'), {self.email})

    def test_published_success_hides_reconciled_queue_without_changing_receipts(self):
        snapshot, history = self.run_campaign()
        self.assertEqual(snapshot['totals']['ok_sends'], 1)
        self.assertIs(history['items'][0]['accounts'][0]['finalized'], True)
        before = upload._sidecar_path(self.sid, 0).read_bytes()
        self.assertEqual(recovery.snapshot()['items'], [])
        self.assertEqual(upload._sidecar_path(self.sid, 0).read_bytes(), before)

    def test_wrong_history_owner_cannot_hide_an_acknowledged_receipt(self):
        self.run_campaign()
        path = next(self.fixture.root.glob('campaign_*.json'))
        history = json.loads(path.read_text(encoding='utf-8'))
        history['items'][0]['accounts'][0]['email'] = 'different@example.invalid'
        path.write_text(json.dumps(history), encoding='utf-8')
        original = path.read_bytes()
        items = recovery.snapshot()['items']
        self.assertEqual(len(items), 1)
        self.assertIs(items[0]['publication_pending'], True)
        self.assertEqual(path.read_bytes(), original)
