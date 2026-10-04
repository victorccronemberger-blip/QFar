"""Declared inert capture fixtures; real ZIP/CSV validation, no physical proof.

The MP4 sentinel is not decodable footage. Only media probing is replaced with
an explicit fixture observation; the inspector and both ZIP/CSV validators run.
Authentication and upload receipts are inert test doubles, never provider proof.
"""
from __future__ import annotations

from dataclasses import replace
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile
from urllib.parse import urlsplit

from moneymin import config, transport, upload
from moneymin.atomic_io import save_json
from moneymin.campaign_types import AccountSpec, CampaignConfig, CampaignLog, TaskSpec
from moneymin.upload_types import ChunkResult, UploadResult

# Missing on archived R15 is a declared feature gap, not a collection error.
if importlib.util.find_spec('moneymin.original_capture') is not None:
    from moneymin import original_capture as original
else:
    original = None

EPOCH = 1_800_000_000
DURATION = 60_000
OWNER = 'owner@example.test'
ORG = 'org_fixture'
TASK = 'task_fixture'
SID = 'declared_inert_capture'


def fixture_pair(root, index=0, *, sid=SID, change=None, count=None):
    """Literal generated native-shaped CSVs; neither IMU nor MP4 is acquired."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    log_id = f'{sid}_{index}'
    start_wall = (EPOCH - 240 + index * 61) * 1000
    start_ns = 9_007_199_254_740_999 + index * 61_000_000_000
    end_ns = start_ns + DURATION * 1_000_000
    created_at = datetime.fromtimestamp(start_wall / 1000, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    metadata = {
        'id': log_id, 'logId': log_id, 'createdAt': created_at,
        'durationMs': DURATION, 'appVersion': 'declared-fixture-original-1.28.0',
        'platform': {'os': 'android', 'version': 33},
        'device': {'systemName': 'Android', 'model': 'DECLARED INERT FIXTURE', 'systemVersion': '13'},
        'video': {'path': f'/declared-fixture/{log_id}.mp4', 'width': 16, 'height': 16, 'rotationDeg': 0},
        'session': {'id': sid}, 'chunk': {'index': index, 'startTimeMs': start_wall, 'endTimeMs': start_wall + DURATION},
        'source': 'ego',
        'timebase': {'clockDomain': 'android_elapsedRealtimeNanos', 'startNs': str(start_ns), 'endNs': str(end_ns),
                     'startSensorTimestampNs': str(start_ns), 'endSensorTimestampNs': str(end_ns),
                     'firstFrameSensorTimestampNs': str(start_ns)},
        'imuDiagnostics': {'sampleCount': 2},
        'artifacts': [{'name': name, 'remoteFilename': f'{log_id}.{name}.csv', 'contentType': 'text/csv'}
                      for name in ('imu', 'frames')],
        'codecActuals': {'mime': 'video/avc', 'width': 16, 'height': 16},
        'cameras': [{'name': 'declared_inert_camera', 'source': 'builtin',
                     'intrinsics_omitted_reason': 'Generated fixture; no acquired camera or calibration'}],
        'fixtureDeclaration': {'generated': True, 'physical_provenance_verified': False},
    }
    if count is not None:
        metadata['session']['chunkCount'] = count
    if change:
        change(metadata)
    imu = f't,ax,ay,az,wx,wy,wz\n{start_ns},0,0,0,0,0,0\n{end_ns},0,0,0,0,0,0\n'
    frames = f'i,ptsNs,dtNs,tNs,key\n0,0,0,{start_ns},1\n1,60000000000,60000000000,{end_ns},0\n'
    media, sidecar = root / f'{log_id}.mp4', root / f'{log_id}.zip'
    media.write_bytes(b'DECLARED INERT NONDECODABLE MP4 SENTINEL ' + log_id.encode())
    with zipfile.ZipFile(sidecar, 'w', compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(f'{log_id}.metadata.json', json.dumps(metadata, indent=2))
        archive.writestr(f'{log_id}.imu.csv', imu)
        archive.writestr(f'{log_id}.frames.csv', frames)
        archive.writestr('declared-extra.txt', b'Preserve this exact optional member and ZIP envelope.')
    return media, sidecar


def fixture_probe(_path):
    return {'has_video': True, 'duration_ms': DURATION, 'width': 16, 'height': 16, 'fps': 30.0, 'codec_name': 'h264'}


def fixture_result(plan, *, done=True, finalized=True, quality_fail=False):
    chunks = [ChunkResult(upload_id=f'fixture_receipt_{i}', chunk_index=i,
                          log_id=f'{plan.session_id}_{i}', blob_path='inert.mp4',
                          size_bytes=c.media.bytes, duration_ms=plan.durations_ms[i],
                          evaluate_result={'checks': [{'id': 'fixture_quality', 'status': 'fail' if quality_fail else 'pass'}]},
                          state='done' if done else 'retry_late') for i, c in enumerate(plan.captures)]
    return UploadResult(plan.session_id, plan.org_key, plan.task_id, chunks=chunks,
                        finalized=finalized, finalize_status=200 if finalized else 503,
                        total_duration_ms=sum(plan.durations_ms))


def fixture_config(plan):
    return CampaignConfig(accounts=[AccountSpec(plan.account_email, plan.org_key)],
                          tasks=[TaskSpec(plan.task_id, 'declared_original', 60, 1800, task_name='Declared fixture task')],
                          original_capture_plan=plan, cleanup_after_upload=False,
                          share_clips=False, realistic_timeline=False, unique_video=False,
                          shuffle_schedule=False, account_workers=1)


class OriginalCaptureWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(original, 'Expected R15 feature gap: original capture campaign engine is absent')
        self.temp = tempfile.TemporaryDirectory(prefix='venom-original-fixture-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'installation'
        self.media_root = self.root / 'library'
        self.patch = patch.multiple(config, DATA_DIR=self.state, MEDIA_DATA_DIR=self.media_root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.clock = patch.object(original.time, 'time', return_value=EPOCH)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.probe = patch('moneymin.sidecar.probe_video', side_effect=fixture_probe)
        self.probe.start()
        self.addCleanup(self.probe.stop)
        self.session = SimpleNamespace(email=OWNER, recording_policy=SimpleNamespace(limits=config.recording_limits),
                                       ensure_auth=Mock(), _check_write_policy=Mock())
        self.auth = patch('moneymin.minute_api.Session.from_email', return_value=self.session)
        self.auth_mock = self.auth.start()
        self.addCleanup(self.auth.stop)

    def plan(self, pairs=None, **kwargs):
        return original.prepare_original_capture_plan(pairs or [fixture_pair(self.root / 'source')],
                    account_email=kwargs.pop('account_email', OWNER), org_key=ORG, task_id=TASK,
                    now=EPOCH, **kwargs)

    def assert_private_error(self, operation, code=None):
        with self.assertRaises(original.OriginalCaptureError) as raised:
            operation()
        if code:
            self.assertEqual(raised.exception.code, code)
        self.assertNotIn(str(self.root), str(raised.exception))
        self.assertNotIn('private-canary', str(raised.exception))
        return raised.exception

    def write_journals(self, plan, *, phase='done', finalized=True, change=None):
        rows = []
        for i, c in enumerate(plan.captures):
            row = {'account_email': plan.account_email, 'org_key': plan.org_key, 'task_id': plan.task_id,
                   'session_id': plan.session_id, 'chunk_index': i, 'expected_chunk_count': len(plan.captures),
                   'log_id': f'{plan.session_id}_{i}', 'filename': f'{plan.session_id}_{i}.mp4',
                   'upload_id': f'fixture_receipt_{i}', 'state': 'done' if finalized else 'completing',
                   'phase': phase, 'finalized': finalized, 'finalize_requested': True,
                   'evaluation_required': True, 'evaluation_verified': True,
                   'register_first': True, 'suppress_per_chunk_catbear': True,
                   'recorded_at': plan.recorded_at[i], 'duration_ms': plan.durations_ms[i],
                   'size_bytes': c.media.bytes, 'sidecar_sha256': c.sidecar.sha256,
                   'original_media_sha256': c.media.sha256, 'original_sidecar_sha256': c.sidecar.sha256,
                   'campaign_context': {'registry_key': fixture_config(plan).tasks[0].registry_key,
                      'clip_uid': plan.session_id, 'task_id': plan.task_id, 'source_mode': 'original',
                      'original_capture_digest': plan.digest, 'original_content_digest': plan.content_digest}}
            if change:
                change(row)
            save_json(self.state / 'sidecars' / upload._sidecar_filename(plan.session_id, i), row)
            rows.append(row)
        return rows

    def test_plan_preserves_exact_bytes_metadata_clock_and_extra_member(self):
        pairs = [fixture_pair(self.root / 'source')]
        before = [path.read_bytes() for path in pairs[0]]
        plan = self.plan(pairs)
        self.assertEqual(plan.session_id, SID)
        self.assertEqual(plan.captures[0].metadata['timebase']['startNs'], '9007199254740999')
        self.assertEqual(plan.recorded_at[0], plan.captures[0].metadata['createdAt'])
        self.assertIn('declared-extra.txt', [m.name for m in plan.captures[0].members])
        self.assertFalse(plan.physical_provenance_verified)
        self.assertEqual([path.read_bytes() for path in pairs[0]], before)
        self.assertFalse(self.state.exists())

    def test_complete_multichunk_group_has_source_verified_total(self):
        plan = self.plan([fixture_pair(self.root / 'source', i, count=2) for i in range(2)])
        self.assertEqual(len(plan.captures), 2)
        self.assertTrue(plan.source_chunk_count_verified)
        self.assertEqual(plan.completeness_binding, 'source')

    def test_missing_source_total_is_only_declared_completeness(self):
        plan = self.plan(expected_chunk_count=1)
        self.assertFalse(plan.source_chunk_count_verified)
        self.assertEqual(plan.completeness_binding, 'declared')

    def test_nonzero_chunk_alone_is_refused(self):
        self.assert_private_error(lambda: self.plan([fixture_pair(self.root / 'source', 1)]))

    def test_missing_final_source_chunk_is_refused(self):
        self.assert_private_error(lambda: self.plan([fixture_pair(self.root / 'source', count=2)]))

    def test_duplicate_or_cross_session_chunks_are_refused(self):
        a = fixture_pair(self.root / 'one')
        b = fixture_pair(self.root / 'two', sid='different_sid')
        for pairs in ([a, a], [a, b]):
            with self.subTest(pairs=len(pairs)):
                self.assert_private_error(lambda: self.plan(pairs))

    def test_wrong_explicit_owner_org_task_are_refused(self):
        for key, value in [('account_email', 'other@example.test'), ('org_key', 'wrong_org'), ('task_id', 'wrong_task')]:
            with self.subTest(key=key):
                pair = fixture_pair(self.root / key, change=lambda m: m.update({key: value}))
                self.assert_private_error(lambda: self.plan([pair]))

    def test_matching_explicit_task_is_source_bound(self):
        plan = self.plan([fixture_pair(self.root / 'source', change=lambda m: m.update(taskId=TASK))])
        self.assertEqual(plan.task_binding, 'source')

    def test_absent_owner_is_declaration_not_ownership_proof(self):
        plan = self.plan()
        self.assertEqual(plan.owner_binding, 'declared')
        self.assertFalse(plan.physical_provenance_verified)

    def test_short_source_meta_fields_must_be_present_and_typed(self):
        changes = [lambda m: m.pop('appVersion'), lambda m: m.update(appVersion=3),
                   lambda m: m['device'].update(model=''), lambda m: m.update(device='private-canary')]
        for i, change in enumerate(changes):
            with self.subTest(i=i):
                self.assert_private_error(lambda: self.plan([fixture_pair(self.root / str(i), change=change)]))

    def test_source_camera_missing_empty_unknown_or_wrong_type_refused_before_auth(self):
        for i, change in enumerate((lambda m: m.pop('cameras'), lambda m: m.update(cameras=[]),
                                   lambda m: m.update(cameras=[{'name': 'fixture', 'source': 'ego'}]),
                                   lambda m: m.update(cameras=[{'name': 'fixture', 'source': 1}]))):
            with self.subTest(i=i):
                self.assert_private_error(lambda: self.plan([fixture_pair(self.root / str(i), change=change)]))
        self.auth_mock.assert_not_called()

    def test_actual_session_write_policy_is_applied_before_new_journal_or_upload(self):
        from moneymin.minute_api import AuthError
        plan = self.plan()
        self.session._check_write_policy.side_effect = AuthError('Declared inert denied camera policy', code='policy')
        with patch.object(upload, 'upload_session') as send:
            with self.assertRaises(AuthError):
                original.run_original_capture_campaign(fixture_config(plan))
        send.assert_not_called()
        self.assertFalse((self.state / 'sidecars' / upload._sidecar_filename(SID)).exists())
        store = json.loads((self.state / 'original_capture_reservations.json').read_bytes())
        self.assertEqual(store['bindings'][SID]['phase'], 'reserved')

    def test_real_csv_validator_rejects_nonmonotonic_frames(self):
        pair = fixture_pair(self.root / 'source')
        self.rewrite_zip(pair[1], lambda name, data: data.replace(b'1,60000000000,60000000000,', b'1,0,0,') if name.endswith('.frames.csv') else data)
        self.assert_private_error(lambda: self.plan([pair]))

    def rewrite_zip(self, path, transform):
        with zipfile.ZipFile(path) as archive:
            entries = [(info.filename, archive.read(info)) for info in archive.infolist()]
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in entries:
                archive.writestr(name, transform(name, data))

    def test_probe_duration_mismatch_and_absent_video_are_refused(self):
        pair = fixture_pair(self.root / 'source')
        for probe in ({'has_video': True, 'duration_ms': 90_000}, {'has_video': False, 'duration_ms': DURATION}):
            with self.subTest(probe=probe):
                self.assert_private_error(lambda: self.plan([pair], probe=lambda _p: probe))

    def test_backlog_and_future_rejected_without_clock_clamp(self):
        pair = fixture_pair(self.root / 'source')
        for now in (EPOCH + 20_000, EPOCH - 1000):
            self.assert_private_error(lambda: original.prepare_original_capture_plan([pair], account_email=OWNER,
                                      org_key=ORG, task_id=TASK, now=now))

    def test_changed_source_and_mutated_plan_are_rejected(self):
        plan = self.plan()
        plan.captures[0].media.path.write_bytes(b'private-canary replaced source')
        self.assert_private_error(lambda: original.verify_original_capture_plan(plan, now=EPOCH))
        plan = self.plan([fixture_pair(self.root / 'second')])
        plan.captures[0].metadata['createdAt'] = 'private-canary'
        self.assert_private_error(lambda: original.verify_original_capture_plan(plan, now=EPOCH))

    def test_context_digest_and_independent_content_digest(self):
        pair = fixture_pair(self.root / 'source')
        a = self.plan([pair])
        b = self.plan([pair], account_email='other@example.test')
        self.assertNotEqual(a.digest, b.digest)
        self.assertEqual(a.content_digest, b.content_digest)
        self.assert_private_error(lambda: original.verify_original_capture_plan(a, account_email=b.account_email, now=EPOCH))
        self.assert_private_error(lambda: original.verify_original_capture_plan(replace(a, digest='0' * 64), now=EPOCH))

    def test_increasing_native_group_clock_without_rebasing(self):
        a = fixture_pair(self.root / 'source')
        b = fixture_pair(self.root / 'source', 1, change=lambda m: m['timebase'].update(startNs='1', endNs='60000000001'))
        self.assert_private_error(lambda: self.plan([a, b]))

    def test_stop_before_effects_never_authenticates_or_uploads(self):
        plan = self.plan()
        with patch.object(upload, 'upload_session') as send:
            log = original.run_original_capture_campaign(fixture_config(plan), should_stop=lambda: True)
        self.assertEqual(log.status, 'stopped')
        self.auth_mock.assert_not_called()
        send.assert_not_called()
        self.assertFalse((self.state / 'original_capture_reservations.json').exists())

    def test_bad_config_before_auth_no_effects(self):
        plan = self.plan()
        for change in (lambda c: c.accounts.append(AccountSpec('other@example.test', ORG)),
                       lambda c: setattr(c, 'unique_video', True), lambda c: setattr(c, 'realistic_timeline', True),
                       lambda c: setattr(c, 'cleanup_after_upload', True), lambda c: setattr(c.tasks[0], 'count', 2)):
            cfg = fixture_config(plan)
            change(cfg)
            self.assert_private_error(lambda: original.run_original_capture_campaign(cfg))
        self.auth_mock.assert_not_called()

    def test_engine_dispatch_preserves_payload_and_never_builds_or_cleans(self):
        from moneymin import campaign
        plan = self.plan()
        before = [(c.media.path.read_bytes(), c.sidecar.path.read_bytes()) for c in plan.captures]
        events = []
        with patch.object(upload, 'upload_session', return_value=fixture_result(plan)) as send, \
             patch.object(campaign, '_new_identity', side_effect=AssertionError('identity builder forbidden')), \
             patch.object(campaign, '_ClipPrefetch', side_effect=AssertionError('prefetch forbidden')), \
             patch.object(campaign, '_cleanup_uploaded_item', side_effect=AssertionError('cleanup forbidden')), \
             patch('moneymin.sent_registry.mark_sent') as index, \
             patch.object(campaign, '_acknowledge_campaign_upload') as acknowledge:
            log = campaign.run_campaign(fixture_config(plan), progress=lambda kind, data: events.append((kind, data)))
        self.assertEqual(log.status, 'done')
        self.assertTrue(log.items[0]['accounts'][0]['ok'])
        kwargs = send.call_args.kwargs
        self.assertFalse(kwargs['normalize'])
        self.assertIsNone(kwargs['profile'])
        self.assertEqual(kwargs['session_id'], SID)
        self.assertEqual(kwargs['recorded_at'], list(plan.recorded_at))
        self.assertEqual(kwargs['sidecar_data'], [before[0][1]])
        self.assertEqual(len(kwargs['original_captures']), 1)
        self.assertEqual(before, [(c.media.path.read_bytes(), c.sidecar.path.read_bytes()) for c in plan.captures])
        self.assertNotIn(str(self.root / 'source'), json.dumps(log.to_dict()))
        self.assertFalse(log.items[0]['source_provenance']['physical_provenance_verified'])
        self.assertEqual(next(payload for kind, payload in events if kind == 'account_done')['credited_seconds'], 60)
        index.assert_called_once()
        acknowledge.assert_called_once()

    def test_stop_during_upload_drains_and_preserves_confirmed_result(self):
        plan = self.plan()
        stop = {'requested': False}
        events = []
        def send(_session, _paths, _org, **kwargs):
            stop['requested'] = True
            kwargs['on_progress']('complete', 'completing', 1)
            kwargs['on_progress']('finalize', 'done', 1)
            return fixture_result(plan)
        with patch.object(upload, 'upload_session', side_effect=send), patch('moneymin.sent_registry.mark_sent'), \
             patch('moneymin.campaign._acknowledge_campaign_upload'):
            log = original.run_original_capture_campaign(fixture_config(plan), should_stop=lambda: stop['requested'],
                      progress=lambda kind, payload: events.append((kind, payload)))
        self.assertEqual(log.status, 'stopped')
        self.assertTrue(log.items[0]['accounts'][0]['ok'])
        self.assertTrue(any(kind == 'account_done' and data['ok'] for kind, data in events))
        self.assertTrue(any(kind == 'campaign_stopped' for kind, _data in events))

    def test_quality_or_finalize_failure_is_not_credited(self):
        for quality, final in ((True, True), (False, False)):
            with self.subTest(quality=quality, finalized=final):
                plan = self.plan([fixture_pair(self.root / str(quality), sid=f'case_{int(quality)}')])
                with patch.object(upload, 'upload_session', return_value=fixture_result(plan, finalized=final, quality_fail=quality)), \
                     patch('moneymin.sent_registry.mark_sent') as index:
                    log = original.run_original_capture_campaign(fixture_config(plan))
                self.assertEqual(log.status, 'error')
                self.assertFalse(log.items[0]['accounts'][0]['ok'])
                index.assert_not_called()

    def test_wrong_authenticated_owner_refused_before_upload(self):
        plan = self.plan()
        self.session.email = 'other@example.test'
        with patch.object(upload, 'upload_session') as send:
            self.assert_private_error(lambda: original.run_original_capture_campaign(fixture_config(plan)))
        send.assert_not_called()

    def test_known_uncertain_receipt_refused_before_auth(self):
        plan = self.plan()
        self.write_journals(plan, phase='create_uncertain', finalized=False,
                            change=lambda r: r.update(upload_id='', create_attempted=True))
        with patch.object(upload, 'upload_session') as send:
            self.assert_private_error(lambda: original.run_original_capture_campaign(fixture_config(plan)))
        self.auth_mock.assert_not_called()
        send.assert_not_called()

    def test_known_wrong_owner_context_or_hash_refused_before_auth(self):
        plan = self.plan()
        for mutate in (lambda r: r.update(account_email='other@example.test'),
                       lambda r: r['campaign_context'].update(original_content_digest='0' * 64),
                       lambda r: r.update(sidecar_sha256='0' * 64)):
            self.write_journals(plan, change=mutate)
            self.assert_private_error(lambda: original.run_original_capture_campaign(fixture_config(plan)))
        self.auth_mock.assert_not_called()

    def test_confirmed_original_receipt_returns_existing_evidence_without_new_auth_or_send(self):
        plan = self.plan()
        self.write_journals(plan)
        with patch.object(upload, 'upload_session') as send:
            log = original.run_original_capture_campaign(fixture_config(plan))
        self.assertEqual(log.status, 'done')
        self.assertTrue(log.items[0]['accounts'][0]['skipped'])
        self.assertTrue(log.items[0]['accounts'][0]['recovered'])
        self.auth_mock.assert_not_called()
        send.assert_not_called()

    def test_supported_known_receipt_uses_only_scoped_pump(self):
        plan = self.plan()
        self.write_journals(plan, phase='awaiting_finalize', finalized=False)
        def resume(_session, **_kwargs):
            return self.write_journals(plan)
        with patch.object(upload, 'upload_session') as send, patch.object(upload, 'pump_pending', side_effect=resume) as pump, \
             patch('moneymin.sent_registry.mark_sent'), patch('moneymin.campaign._acknowledge_campaign_upload'):
            log = original.run_original_capture_campaign(fixture_config(plan))
        self.assertTrue(log.items[0]['accounts'][0]['ok'])
        send.assert_not_called()
        self.assertEqual(pump.call_args.kwargs['session_ids'], {SID})
        self.assertEqual(pump.call_args.kwargs['account_email'], OWNER)
        self.assertEqual(pump.call_args.kwargs['required_org_key'], ORG)

    def test_persistent_reservation_blocks_retargeted_content_and_uncertain_restart(self):
        plan = self.plan()
        with patch.object(upload, 'upload_session', return_value=fixture_result(plan, done=False, finalized=False)):
            original.run_original_capture_campaign(fixture_config(plan))
        self.auth_mock.reset_mock()
        other = self.plan([(plan.captures[0].media.path, plan.captures[0].sidecar.path)], account_email='other@example.test')
        self.assert_private_error(lambda: original.run_original_capture_campaign(fixture_config(other)))
        self.assert_private_error(lambda: original.run_original_capture_campaign(fixture_config(plan)))
        self.auth_mock.assert_not_called()

    def test_observer_failure_keeps_completed_result_in_private_recovery_log(self):
        plan = self.plan()
        def observer(kind, payload):
            if kind == 'account_done':
                raise RuntimeError('declared observer failure')
        with patch.object(upload, 'upload_session', return_value=fixture_result(plan)), \
             patch('moneymin.sent_registry.mark_sent'), patch('moneymin.campaign._acknowledge_campaign_upload'):
            with self.assertRaises(RuntimeError) as raised:
                original.run_original_capture_campaign(fixture_config(plan), progress=observer)
        log = raised.exception._moneymin_campaign_log
        self.assertTrue(log.items[0]['accounts'][0]['ok'])
        self.assertTrue(log._path.exists())

    def test_lock_refuses_concurrent_writer_and_never_removes_existing_lock(self):
        plan = self.plan()
        self.state.mkdir()
        lock = self.state / '.original_capture.lock'
        lock.write_bytes(b'existing owner lock private-canary')
        from moneymin.operation_lease import operation_lease
        held, release = threading.Event(), threading.Event()
        def hold_lease():
            with operation_lease(lock):
                held.set()
                release.wait(5)
        worker = threading.Thread(target=hold_lease)
        worker.start()
        try:
            self.assertTrue(held.wait(2))
            self.assert_private_error(lambda: original.run_original_capture_campaign(fixture_config(plan)))
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(lock.read_bytes(), b'existing owner lock private-canary')
        self.auth_mock.assert_not_called()

    def test_org_unbound_first_phase_uses_real_shared_validation_without_auth(self):
        pair = fixture_pair(self.root / 'source', change=lambda m: m.update(org_key=ORG))
        inspected = original.inspect_original_capture_group([pair], account_email=OWNER, task_id=TASK, now=EPOCH)
        self.assertEqual(inspected.declared_org_key, ORG)
        self.assertEqual(inspected.session_id, SID)
        self.assertFalse(hasattr(inspected, 'digest'))
        self.assertFalse(self.state.exists())
        self.auth_mock.assert_not_called()
        plan = self.plan([pair])
        self.assertEqual(inspected.captures, plan.captures)

    def test_native_clock_text_grammar_and_explicit_totals_reject_coercion(self):
        for i, change in enumerate((lambda m: m.update(createdAt='2027-01-15T07:56:00.000000Z'),
                                    lambda m: m.update(createdAt='2027-01-15T07:56:00.000+00:00'),
                                    lambda m: m.update(expectedChunkCount=True),
                                    lambda m: m.update(expectedChunkCount='1'))):
            with self.subTest(i=i):
                self.assert_private_error(lambda: self.plan([fixture_pair(self.root / str(i), change=change)]))

    def test_native_wall_and_sensor_domains_are_preserved_without_unproven_equality(self):
        plan = self.plan([fixture_pair(self.root / 'source', change=lambda m: m['chunk'].update(endTimeMs=m['chunk']['startTimeMs'] + 59_990))])
        self.assertEqual(plan.captures[0].metadata['chunk']['endTimeMs'] - plan.captures[0].metadata['chunk']['startTimeMs'], 59_990)
        self.assertEqual(plan.durations_ms[0], 60_000)
        self.assertFalse(plan.public_summary()['cross_clock_conversion_verified'])

    def test_same_session_device_alias_switch_is_refused(self):
        pairs = [fixture_pair(self.root / 'source', 0),
                 fixture_pair(self.root / 'source', 1, change=lambda m: m['device'].update(model='other-declared-fixture'))]
        self.assert_private_error(lambda: self.plan(pairs), 'identity_conflict')

    def test_changed_source_with_same_size_and_restored_mtime_is_refused(self):
        import os
        plan = self.plan()
        path = plan.captures[0].media.path
        stamp = path.stat()
        raw = path.read_bytes()
        path.write_bytes(b'X' + raw[1:])
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assert_private_error(lambda: original.verify_original_capture_plan(plan, now=EPOCH))

    def test_archive_growth_after_inspection_is_bounded_and_rejected(self):
        plan = self.plan()
        with plan.captures[0].sidecar.path.open('ab') as stream:
            stream.write(b'private-canary appended after inspection')
        self.assert_private_error(lambda: original._read_archive(plan.captures[0]), 'source_changed')

    def test_incomplete_known_group_never_authenticates_or_creates(self):
        plan = self.plan([fixture_pair(self.root / 'source', i, count=2) for i in range(2)])
        self.write_journals(plan)
        (self.state / 'sidecars' / upload._sidecar_filename(SID, 1)).unlink()
        self.assert_private_error(lambda: original.run_original_capture_campaign(fixture_config(plan)))
        self.auth_mock.assert_not_called()

    def test_session_owner_source_before_auth_and_remote_policy_recheck(self):
        plan = self.plan()
        self.session.recording_policy.limits = lambda: {**config.recording_limits(), 'min_duration_ms': 90_000}
        with patch.object(upload, 'upload_session') as send:
            self.assert_private_error(lambda: original.run_original_capture_campaign(fixture_config(plan)), 'policy')
        send.assert_not_called()

    def test_stop_before_inspection_never_reads_original_group(self):
        plan = self.plan()
        with patch.object(original, 'verify_original_capture_plan', side_effect=AssertionError('inspection forbidden after Stop')):
            log = original.run_original_capture_campaign(fixture_config(plan), should_stop=lambda: True)
        self.assertEqual(log.status, 'stopped')
        self.auth_mock.assert_not_called()

    def test_stop_during_pure_inspection_cancels_before_auth_or_reservation(self):
        plan = self.plan([fixture_pair(self.root / 'source', i, count=2) for i in range(2)])
        source_inspector = original.inspect_original_capture
        stop = {'requested': False}
        def inspect(*args, **kwargs):
            value = source_inspector(*args, **kwargs)
            stop['requested'] = True
            return value
        with patch.object(original, 'inspect_original_capture', side_effect=inspect):
            log = original.run_original_capture_campaign(fixture_config(plan), should_stop=lambda: stop['requested'])
        self.assertEqual(log.status, 'stopped')
        self.auth_mock.assert_not_called()
        self.assertFalse((self.state / 'original_capture_reservations.json').exists())

    def test_resumed_reservation_never_downgrades_creation_history_on_auth_failure(self):
        plan = self.plan()
        self.write_journals(plan, phase='awaiting_finalize', finalized=False)
        self.session.ensure_auth.side_effect = RuntimeError('declared inert auth failure')
        with self.assertRaises(RuntimeError):
            original.run_original_capture_campaign(fixture_config(plan))
        store = json.loads((self.state / 'original_capture_reservations.json').read_bytes())
        self.assertEqual(store['bindings'][SID]['phase'], 'creation_attempted')

    def test_known_zip_receipt_pre_auth_inspection_never_creates_or_migrates_journals(self):
        plan = self.plan()
        saved = self.state / 'sidecars' / upload._sidecar_filename(SID, 0)
        archive = saved.with_suffix('.data.zip')
        def patch_receipt(row):
            row.update(transport_artifact='sidecar', conflict_action='complete',
                       sidecar_size_bytes=plan.captures[0].sidecar.bytes,
                       sidecar_data_path=str(archive.resolve()))
        self.write_journals(plan, phase='registered', finalized=False, change=patch_receipt)
        archive.write_bytes(plan.captures[0].sidecar.path.read_bytes())
        before = saved.read_bytes()
        with patch.object(upload, 'sidecars_dir', side_effect=AssertionError('Migration/creation forbidden during pre-auth inspection')):
            rows, mode = original._known_group(plan, fixture_config(plan).tasks[0].registry_key)
        self.assertEqual(mode, 'resume')
        self.assertEqual(len(rows), 1)
        self.assertEqual(saved.read_bytes(), before)
        self.auth_mock.assert_not_called()

    def test_bad_known_zip_hash_is_refused_without_auth(self):
        plan = self.plan()
        saved = self.state / 'sidecars' / upload._sidecar_filename(SID, 0)
        archive = saved.with_suffix('.data.zip')
        self.write_journals(plan, phase='registered', finalized=False,
             change=lambda r: r.update(transport_artifact='sidecar', conflict_action='complete',
                     sidecar_size_bytes=plan.captures[0].sidecar.bytes, sidecar_data_path=str(archive.resolve())))
        archive.write_bytes(b'private-canary damaged existing ZIP')
        self.assert_private_error(lambda: original.run_original_capture_campaign(fixture_config(plan)))
        self.auth_mock.assert_not_called()

    def test_observer_mutated_source_before_upload_is_refused_by_actual_uploader(self):
        plan = self.plan()
        def observer(kind, _payload):
            if kind == 'account_start':
                plan.captures[0].media.path.write_bytes(b'private-canary observer mutated original')
        # No receipt/network adapter is supplied. Reinspection must reject
        # after observer changes and before any provider call or new journal.
        with patch.object(upload, 'probe_video', side_effect=fixture_probe):
            with self.assertRaises(upload.UploadError):
                original.run_original_capture_campaign(fixture_config(plan), progress=observer)
        self.assertFalse((self.state / 'sidecars' / upload._sidecar_filename(SID, 0)).exists())

    def integrated_protocol(self, plan, *, quality='pass', finalize_status=204, control=None):
        """Real core/uploader/transport/runner with inert provider/HTTP adapters."""
        from moneymin.web.runner import CampaignRunner
        runner = CampaignRunner()
        api, blocks, committed = [], {}, {}
        reached = threading.Event()
        controls = {'triggered': False}

        class InertProtocol:
            email = OWNER
            from moneymin.service_policy import RecordingPolicy
            from moneymin.minute_api import Session as AuthenticSession
            recording_policy = RecordingPolicy(1, 60_000, 1_800_000, 14_400_000)
            device_camera_allowed = True
            initialization_errors = {}
            _initialization_causes = {}
            _check_write_policy = AuthenticSession._check_write_policy
            _check_local_restrictions = AuthenticSession._check_local_restrictions
            _check_recording_geo = AuthenticSession._check_recording_geo

            def warmup(self):
                # Current service observations below are declared inert fixture
                # data. The actual write-policy evaluator remains unmodified.
                pass

            def org_state(self, _org):
                return {'blocked': False, 'userState': 'active', 'cameraSources': ['built-in', 'external']}

            def recording_geo(self, _org):
                return {'recordingAuthorization': 'APPROVED', 'canUpload': True, 'blockedReason': None}

            def ensure_auth(self, **_kwargs):
                pass

            def request(self, method, path, body=None):
                self._check_write_policy(method, path, body)
                api.append((method, path, body))
                if path.startswith('/api/v1/uploads?'):
                    return 201, json.dumps({'id': 'inert_receipt_' + str(body['meta']['chunk_index']),
                                            'status': 'initiated', 'meta': {}})
                if path == '/api/v1/storage/sas/blobs':
                    return 200, json.dumps({'signed_urls': [{'filename': f['filename'],
                        'blob_url': 'https://declared-fixture.invalid/' + f['filename'],
                        'expires_at': '2030-01-01T00:00:00Z'} for f in body['files']]})
                if path.endswith('/complete'):
                    return 200, json.dumps({'id': path.split('/')[-2], 'status': 'uploaded', 'meta': {}})
                if path.endswith('/evaluate'):
                    return 200, json.dumps({'upload_id': path.split('/')[-2], 'checks': [
                        {'id': 'declared_inert_quality', 'label': 'Declared fixture', 'detail': 'No provider called', 'status': quality}]})
                if path.endswith('/finalize'):
                    return finalize_status, ''
                raise AssertionError('Unexpected inert provider operation: ' + method + ' ' + path)

            def put(self, url, **kwargs):
                name = urlsplit(url).path.lstrip('/')
                if 'comp=blocklist' in url:
                    committed[name] = b''.join(blocks.get(name, []))
                else:
                    blocks.setdefault(name, []).append(kwargs['data'])
                    if control and not controls['triggered'] and name.endswith('.mp4'):
                        controls['triggered'] = True
                        runner.pause() if control == 'pause' else runner.stop()
                        reached.set()
                return SimpleNamespace(status_code=201, text='')

        protocol = InertProtocol()
        with ExitStack() as stack:
            stack.enter_context(patch('moneymin.minute_api.Session.from_email', return_value=protocol))
            stack.enter_context(patch.object(upload, 'probe_video', side_effect=fixture_probe))
            stack.enter_context(patch.object(transport, '_kind', 'curl'))
            stack.enter_context(patch.object(transport, '_cffi', protocol))
            stack.enter_context(patch.object(transport, '_impersonate', None))
            stack.enter_context(patch.object(transport, 'BLOCK_SIZE', 64))
            stack.enter_context(patch.object(upload.time, 'sleep', return_value=None))
            runner.start(fixture_config(plan))
            if control:
                self.assertTrue(reached.wait(3), 'Inert transport control point was not reached')
                if control == 'pause':
                    # The real checkpoint must block further commits until Resume.
                    self.assertTrue(runner.pause_requested)
                    self.assertNotIn(f'{plan.session_id}_0.mp4', committed)
                    runner.resume()
            runner._thread.join(5)
            self.assertFalse(runner._thread.is_alive(), 'Original capture campaign did not drain')
        histories = list(self.state.glob('campaign_*.json'))
        self.assertEqual(len(histories), 1)
        history = json.loads(histories[0].read_bytes())
        return runner, history, api, committed

    def test_real_core_upload_transport_multichunk_e2e_byte_bodies_journals_counters(self):
        pairs = [fixture_pair(self.root / 'source', i, count=2) for i in range(2)]
        plan = self.plan(pairs)
        originals = [(m.read_bytes(), z.read_bytes()) for m, z in pairs]
        runner, log, api, committed = self.integrated_protocol(plan)
        self.assertEqual((runner.state, log['status']), ('done', 'done'))
        self.assertEqual((runner.ok_sends, runner.failed_sends, runner.done_sends), (1, 0, 1))
        self.assertEqual(runner.account_seconds[OWNER], 120)
        creates = [body for _method, path, body in api if path.startswith('/api/v1/uploads?')]
        self.assertEqual(len(creates), 2)
        for i, body in enumerate(creates):
            self.assertEqual(body['session_id'], SID)
            self.assertEqual(body['log_id'], f'{SID}_{i}')
            self.assertEqual(body['recorded_at'], plan.recorded_at[i])
            self.assertEqual(body['meta']['device'], {'model': 'DECLARED INERT FIXTURE'})
            self.assertEqual(body['meta']['platform'], {'os': 'android'})
            self.assertEqual(body['meta']['appVersion'], plan.captures[i].metadata['appVersion'])
            self.assertEqual(committed[f'{SID}_{i}.mp4'], originals[i][0])
            self.assertEqual(committed[f'{SID}_{i}.data.zip'], originals[i][1])
        self.assertEqual(originals, [(m.read_bytes(), z.read_bytes()) for m, z in pairs])
        rows, mode = original._known_group(plan, fixture_config(plan).tasks[0].registry_key)
        self.assertEqual(mode, 'confirmed')
        self.assertTrue(all(row['campaign_reconciled'] for row in rows))
        self.assertTrue(all(row['campaign_context']['history_name'] == runner.log_path for row in rows))
        self.assertEqual(next(body for _method, path, body in api if path.endswith('/finalize')), {'expected_chunk_count': 2})
        self.assertFalse(log['items'][0]['source_provenance']['physical_provenance_verified'])
        self.assertNotIn(str(self.root / 'source'), json.dumps(log))
        from moneymin import campaign_evidence
        current = campaign_evidence.current_result(log['items'][0], log['items'][0]['accounts'][0], runner.log_path, {SID: rows})
        self.assertEqual(current['status'], 'confirmed')

    def test_real_core_upload_transport_stop_drains_complete_finalize_and_runner_stopped(self):
        plan = self.plan()
        runner, log, api, committed = self.integrated_protocol(plan, control='stop')
        self.assertEqual((runner.state, log['status']), ('stopped', 'stopped'))
        self.assertEqual(runner.ok_sends, 1)
        self.assertTrue(any(path.endswith('/complete') for _m, path, _b in api))
        self.assertTrue(any(path.endswith('/finalize') for _m, path, _b in api))
        self.assertEqual(len(committed), 2)

    def test_real_core_upload_transport_pause_resumes_without_identity_or_byte_changes(self):
        plan = self.plan()
        runner, log, _api, committed = self.integrated_protocol(plan, control='pause')
        self.assertEqual((runner.state, log['status']), ('done', 'done'))
        self.assertFalse(runner.pause_requested)
        self.assertEqual(committed[f'{SID}_0.data.zip'], plan.captures[0].sidecar.path.read_bytes())

    def test_real_core_upload_transport_quality_fail_preserves_receipts_without_finalize_credit(self):
        plan = self.plan()
        runner, log, api, committed = self.integrated_protocol(plan, quality='fail')
        self.assertEqual((runner.state, log['status']), ('error', 'error'))
        self.assertEqual((runner.ok_sends, runner.failed_sends), (0, 1))
        self.assertFalse(any(path.endswith('/finalize') for _m, path, _b in api))
        self.assertEqual(len(committed), 2)
        row = upload.load_sidecar(SID)
        self.assertEqual(row['phase'], 'evaluation_review')
        self.assertEqual(row['upload_id'], 'inert_receipt_0')

    def test_real_core_upload_transport_finalize_failure_keeps_pending_receipt(self):
        plan = self.plan()
        runner, log, api, committed = self.integrated_protocol(plan, finalize_status=503)
        self.assertEqual((runner.state, log['status']), ('error', 'error'))
        self.assertEqual((runner.ok_sends, runner.failed_sends), (0, 1))
        self.assertEqual(sum(path.endswith('/finalize') for _m, path, _b in api), 3)
        self.assertEqual(len(committed), 2)
        rows, mode = original._known_group(plan, fixture_config(plan).tasks[0].registry_key)
        self.assertEqual(mode, 'resume')
        self.assertEqual(rows[0]['upload_id'], 'inert_receipt_0')


if __name__ == '__main__':
    unittest.main()
