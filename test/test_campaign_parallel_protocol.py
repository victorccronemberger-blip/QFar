"""API/runner/engine/account/driver/transport integration with inert artifacts.

Auth, catalog, capture preparation, cuts, profiles and archive validation are
declared fixtures. No sensor/media validity or provider acceptance is certified.
"""
from dataclasses import replace
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from moneymin import campaign, device_profile, sent_registry, transport, upload
from moneymin.web import runner, server
import test_campaign_end_to_end as fixtures


class CampaignParallelProtocolTests(unittest.TestCase):
    def execute(self, control):
        real_mark, real_sent, real_all = sent_registry.mark_sent, sent_registry.sent_emails, sent_registry.is_sent_to_all
        fixture = fixtures.CampaignEndToEndTests('test_success_matches_polling_and_persisted_history')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        stack, root = fixture.stack, fixture.root
        (root / 'journals').mkdir()
        fixture.emails.append('queued@example.invalid')
        video_bytes = b'Declared video protocol fixture, not a recording.'
        zip_bytes = b'Declared ZIP protocol fixture, not sensor observations.'
        (root / 'fixture.mp4').write_bytes(video_bytes)
        fixture.prepare.side_effect = lambda *a, **k: {
            'duration_ms': 120_000, 'video_path': str(root / 'fixture.mp4'),
            'imu_real': True, 'imu_csv': 'opaque inert IMU fixture'}
        fixture.send.side_effect = fixtures.REAL_UPLOAD_TO_ACCOUNT
        guard = threading.Lock()
        entered, release = threading.Event(), threading.Event()
        blocked = threading.Event()
        initial_owners, blocked_threads, wire, closed = set(), set(), [], []
        cleanup_observations = []
        instance = fixture.instance
        real_checkpoint = instance._checkpoint

        if control in {'stop', 'drain'}:
            def capture_cleanup(item, work_dir, **kwargs):
                with guard:
                    wire_before_cleanup = list(wire)
                rows_before_cleanup = [
                    upload.load_sidecar(f'protocol-{index}', chunk)
                    for index in range(2) for chunk in range(2)
                ]
                cleanup_observations.append((wire_before_cleanup, rows_before_cleanup))
                return {'files': 0, 'bytes': 0, 'errors': [], 'protected': 0}
            fixture.cleanup.side_effect = capture_cleanup

        def checkpoint():
            if instance.pause_requested:
                with guard:
                    blocked_threads.add(threading.current_thread().name)
                    if len([name for name in blocked_threads if name.startswith('moneymin-upload')]) >= 2:
                        blocked.set()
            return real_checkpoint()

        def record(kind, identity, body):
            with guard:
                wire.append((kind, identity, body, instance.pause_requested))

        class FixtureSession:
            _live = True
            recording_policy = None
            _moneymin_pending_pumped = False

            def __init__(self, email):
                self.email = email

            def ensure_auth(self, **kwargs):
                return {'email': self.email}

            def warmup(self):
                pass

            def all_tasks(self, org_key=None):
                return [{'id': 'task', 'name': 'Furniture Assembly', 'scenario': 'assembling furniture'}]

            def request(self, method, path, body=None):
                kind = ('create' if path.startswith('/api/v1/uploads?') else
                        'sas' if path == '/api/v1/storage/sas/blobs' else
                        'complete' if path.endswith('/complete') else
                        'evaluate' if path.endswith('/evaluate') else
                        'finalize' if path.endswith('/finalize') else 'unexpected')
                record(kind, self.email, body)
                if kind == 'create':
                    return 201, json.dumps({'id': 'accepted-' + body['log_id'], 'status': 'initiated', 'meta': {}})
                if kind == 'sas':
                    return 200, json.dumps({'signed_urls': [
                        {'filename': row['filename'], 'blob_url': 'https://blob.invalid/' + row['filename'], 'expires_at': '2030-01-01T00:00:00Z'}
                        for row in body['files']]})
                if kind == 'complete':
                    return 200, json.dumps({'id': path.split('/')[-2], 'status': 'uploaded', 'meta': {}})
                if kind == 'evaluate':
                    return 200, json.dumps({'upload_id': path.split('/')[-2],
                        'checks': [{'id': 'inert', 'label': 'Declared inert quality', 'status': 'pass', 'detail': None}]})
                if kind == 'finalize':
                    return 204, ''
                raise AssertionError('Unexpected fixture provider operation')

        class FixtureBlobClient:
            def put(self, url, **kwargs):
                filename = Path(urlsplit(url).path).name
                kind = 'blocklist' if 'comp=blocklist' in url else 'block'
                record(kind, filename, kwargs['data'])
                sid = filename.split('_')[0]
                if kind == 'block' and filename.endswith('_0.mp4'):
                    first = False
                    with guard:
                        if sid not in initial_owners:
                            initial_owners.add(sid)
                            first = True
                            if len(initial_owners) == 2:
                                entered.set()
                    if first and not release.wait(5):
                        raise AssertionError('Fixture release timeout')
                return SimpleNamespace(status_code=201, text='')

            def close(self):
                closed.append(True)

        client = FixtureBlobClient()
        client.Session = lambda **kwargs: FixtureBlobClient()
        probe = {'duration_ms': 60_000, 'fps': 30, 'width': 1440, 'height': 1080,
                 'codec': 'h264', 'has_video': True, 'has_audio': True}

        def cut(source, destination, *args):
            destination.write_bytes(video_bytes)

        def identity(duration, email, **kwargs):
            sid = f'protocol-{fixture.emails.index(email)}'
            return sid, sid + '_0', device_profile.format_recorded_at(time.time() - 300)

        contexts = (
            patch.object(server, '_list_accounts', return_value=[{'email': email} for email in fixture.emails]),
            patch.object(server.Session, 'from_email', side_effect=lambda email, **kwargs: FixtureSession(email)),
            patch.object(runner, 'run_campaign', side_effect=lambda cfg, **kw: campaign.run_campaign(
                replace(cfg, realistic_timeline=False, allow_new_accounts=True,
                        shuffle_schedule=False, account_workers=2,
                        account_gap_s=0, account_retry_s=0), **kw)),
            patch.object(instance, '_checkpoint', side_effect=checkpoint),
            patch.object(campaign.org_policy, 'account_kind', return_value='claru'),
            patch.object(campaign.device_profile, 'get_profile', side_effect=lambda email: SimpleNamespace(
                email=email, frames_gop=30, uptime_ns_at=lambda wall: 1,
                upload_device_meta=lambda: {'fixture': True}, upload_platform_meta=lambda: {'fixture': True})),
            patch.object(campaign, '_new_identity', side_effect=identity),
            patch.object(campaign, '_chunk_plan', return_value=[(0, 60_000), (60_000, 60_000)]),
            patch.object(campaign, '_cut_video_chunk', side_effect=cut),
            patch.object(campaign, '_slice_imu_csv', return_value=('opaque inert IMU fixture', 1)),
            patch.object(campaign, 'build_frames_csv_from_video', return_value='opaque inert frames fixture'),
            patch.object(campaign, '_build_sidecar', return_value=zip_bytes),
            patch.object(campaign, 'probe_video', return_value=probe),
            patch.object(upload, 'probe_video', return_value=probe),
            patch.object(upload, '_probe_duration_ms', return_value=60_000),
            patch.object(upload, '_validate_sidecar_zip', return_value={'fixture': True}),
            patch('moneymin.validate.validate_upload_meta', return_value=[]),
            patch.object(upload, 'sidecars_dir', return_value=root / 'journals'),
            patch.object(campaign.sent_registry, 'mark_sent', side_effect=real_mark),
            patch.object(campaign.sent_registry, 'sent_emails', side_effect=real_sent),
            patch.object(campaign.sent_registry, 'is_sent_to_all', side_effect=real_all),
            patch.object(transport, '_kind', 'curl'), patch.object(transport, '_cffi', client),
            patch.object(transport, '_impersonate', None), patch.object(transport, 'BLOCK_SIZE', 8),
        )
        for context in contexts:
            stack.enter_context(context)
        started = fixture.client.post('/api/campaigns', json=fixture.body)
        self.assertEqual(started.status_code, 200, started.get_json())
        try:
            self.assertTrue(entered.wait(5), 'two actual account drivers entered file transport')
            endpoint = ('/api/campaigns/pause' if control == 'pause' else
                        '/api/campaigns/drain' if control == 'drain' else '/api/campaigns/stop')
            if control == 'drain':
                self.assertEqual(fixture.client.post('/api/campaigns/pause').status_code, 200)
            self.assertEqual(fixture.client.post(endpoint).status_code, 200)
            if control == 'drain':
                self.assertIs(fixture.client.post(endpoint).get_json()['ready'], False)
                self.assertFalse(instance.pause_requested)
            release.set()
            if control == 'pause':
                self.assertTrue(blocked.wait(2), 'both account drivers wait at their real progress callback')
                self.assertFalse(any(paused for _, _, _, paused in wire))
                self.assertEqual(sum(kind == 'create' for kind, _, _, _ in wire), 2)
                self.assertFalse(any(kind in {'complete', 'finalize'} for kind, _, _, _ in wire))
                self.assertEqual(fixture.client.post('/api/campaigns/resume').status_code, 200)
            instance._thread.join(5)
        finally:
            release.set()
            if instance.pause_requested:
                instance.resume()
            instance._thread.join(5)
        snapshot, history = fixture.finish()
        expected_accounts = fixture.emails if control == 'pause' else fixture.emails[:2]
        count = len(expected_accounts)
        self.assertEqual((snapshot['state'], history['status']),
                         ('done', 'done') if control == 'pause' else ('stopped', 'stopped'),
                         {'runner_error': snapshot.get('error'), 'issues': history.get('issues')})
        self.assertEqual(snapshot['totals']['ok_sends'], count)
        self.assertEqual({row['email'] for row in history['items'][0]['accounts']}, set(expected_accounts))
        self.assertEqual(sum(kind == 'create' for kind, _, _, _ in wire), count * 2)
        self.assertEqual(sum(kind == 'complete' for kind, _, _, _ in wire), count * 2)
        self.assertEqual(sum(kind == 'evaluate' for kind, _, _, _ in wire), count * 2)
        self.assertEqual(sum(kind == 'finalize' for kind, _, _, _ in wire), count)
        self.assertEqual(len(closed), count * 2)
        self.assertFalse(any(paused for _, _, _, paused in wire))
        for index, email in enumerate(expected_accounts):
            for chunk in range(2):
                row = upload.load_sidecar(f'protocol-{index}', chunk)
                self.assertEqual(row['account_email'], email)
                self.assertEqual(row['upload_id'], f'accepted-protocol-{index}_{chunk}')
                self.assertIs(row['finalized'], True)
                self.assertIs(row['evaluation_verified'], True)
                self.assertIs(row['campaign_reconciled'], True)
                for suffix, original in (('mp4', video_bytes), ('data.zip', zip_bytes)):
                    filename = f'protocol-{index}_{chunk}.{suffix}'
                    data = b''.join(body for kind, identity, body, _ in wire
                                    if kind == 'block' and identity == filename)
                    self.assertEqual(data, original)
                    self.assertEqual(sum(kind == 'blocklist' and identity == filename
                                         for kind, identity, _, _ in wire), 1)
        fixture.cleanup.assert_called_once()
        if control in {'stop', 'drain'}:
            self.assertEqual(len(cleanup_observations), 1)
            wire_before_cleanup, rows_before_cleanup = cleanup_observations[0]
            self.assertEqual(sum(kind == 'create' for kind, _, _, _ in wire_before_cleanup), 4)
            self.assertEqual(sum(kind == 'complete' for kind, _, _, _ in wire_before_cleanup), 4)
            self.assertEqual(sum(kind == 'evaluate' for kind, _, _, _ in wire_before_cleanup), 4)
            self.assertEqual(sum(kind == 'finalize' for kind, _, _, _ in wire_before_cleanup), 2)
            self.assertEqual({identity for kind, identity, _, _ in wire_before_cleanup
                              if kind == 'create'}, set(fixture.emails[:2]))
            self.assertTrue(all(row['finalized'] is True
                                and row['evaluation_verified'] is True
                                and row['campaign_reconciled'] is True
                                for row in rows_before_cleanup))
        if control == 'drain':
            self.assertIs(fixture.client.post('/api/campaigns/drain').get_json()['ready'], True)
            self.assertEqual(fixture.client.post('/api/campaigns', json=fixture.body).status_code, 409)

    def test_pause_then_resume_two_active_accounts_and_queued_successor(self):
        self.execute('pause')

    def test_stop_drains_two_active_multichunk_sessions_without_starting_successor(self):
        self.execute('stop')

    def test_close_drains_two_paused_multichunk_sessions_without_starting_successor(self):
        self.execute('drain')
