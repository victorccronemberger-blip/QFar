"""Byte-bound local fixtures for runner, reconciliation and IMU chunk fixes.

Video probing/PTS and the transport result are declared fixtures. Cache marker
checks, preparation, source IMU resampling, sidecars, journals and cleanup are
real local operations. No authentication, decoding or remote service is used.
"""
from contextlib import ExitStack
import copy
import csv
import io
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from moneymin import campaign, config, content_provenance, device_profile, ego4d, sidecar, upload
from moneymin.campaign_types import AccountSpec, CampaignConfig
from moneymin.web import runner


HEADER = 'canonical_timestamp_ms,gyro_x,gyro_y,gyro_z,accl_x,accl_y,accl_z\n'
UPTIME_NS = 1_000_000_000_000


class WireFixtures(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='ego4d-wire-regression-'))).resolve()
        self.stack.enter_context(patch.multiple(config, DATA_DIR=self.root / 'state',
            MEDIA_DATA_DIR=self.root / 'library', SECRETS_DIR=self.root / 'secrets', VIDEOS_DIR=self.root / 'recordings'))
        self.journals = self.root / 'journals'
        self.journals.mkdir(parents=True, exist_ok=True)
        self.stack.enter_context(patch.object(upload, 'sidecars_dir', return_value=self.journals))
        self.stack.enter_context(patch('socket.socket.connect', side_effect=AssertionError('Network forbidden')))
        self.stack.enter_context(patch.object(campaign.Session, 'from_email', side_effect=AssertionError('Auth forbidden')))
        self.account = AccountSpec('wire-fixture@example.invalid', 'fixture-org')

    def complete_journal(self, sid, clip='fixture-clip', lineage=None):
        context = {'registry_key': 'fixture-key', 'clip_uid': clip, 'task_id': 'fixture-task'}
        if lineage is not None:
            context['content_provenance'] = lineage
        row = dict(session_id=sid, chunk_index=0, expected_chunk_count=1,
                   account_email=self.account.email, org_key=self.account.org_key,
                   task_id='fixture-task', upload_id='fixture-upload-' + sid,
                   state='done', phase='done', finalized=True, campaign_context=context)
        upload.save_sidecar(row)
        return row

    def lineage(self, sid):
        value = {'session_id': sid, 'task_id': 'fixture-task', 'org_key': self.account.org_key,
                 'confirmed_delivery': False, 'physical_provenance_verified': False}
        value['delivery_binding_sha256'] = content_provenance.canonical_digest(value)
        return value


class RunnerLibraryAndPendingTests(WireFixtures):
    def cached_item(self):
        work = config.MEDIA_DATA_DIR / 'ego4d'
        work.mkdir(parents=True)
        clip = {'clip_uid': 'fixture-clip', 'exported_clip_uid': 'fixture-clip',
                'parent_video_uid': 'fixture-parent', 'window_s': (0.0, 60.0),
                'parent_start_sec': '0', 'parent_end_sec': '60', 'source': 'ego4d'}
        video = {'video_uid': 'fixture-parent', 'has_imu': True}
        source = work / 'fixture-clip.mp4'
        native = work / 'fixture-clip_native.mp4'
        for path, tag in ((source, b'source'), (native, b'prepared')):
            path.write_bytes(b'\x00\x00\x00\x20ftyp' + tag + b'\x00' * (1024 * 1024 + 1))
        sensor = work / 'fixture-parent_imu.csv'
        sensor.write_text(HEADER + ''.join(f'{i},1,2,3,4,5,6\n' for i in range(0, 60_001, 10)), encoding='utf-8')
        marker = native.with_name(native.name + '.source.json')
        marker.write_text(json.dumps(campaign._native_cache_marker(source, native, None, 60.0)), encoding='utf-8')
        probe = {'duration_ms': 60_000, 'fps': 30.0, 'has_video': True, 'width': 1440, 'height': 1080}
        self.stack.enter_context(patch.object(campaign, 'probe_video', return_value=probe))
        self.stack.enter_context(patch.object(sidecar, 'probe_video', return_value=probe))
        self.encode = self.stack.enter_context(patch.object(campaign, '_ffmpeg_run', side_effect=AssertionError('Cached item re-encoded')))
        self.download = self.stack.enter_context(patch.object(ego4d, 'download_clip', side_effect=AssertionError('Cached item downloaded')))
        self.stack.enter_context(patch.object(campaign, '_ego_clip_inputs', return_value=(clip, video)))
        self.assertEqual(campaign.ego_clip_cache_state(clip, work), 'ready')
        return work, clip, video, native, marker

    def test_runner_prepares_from_reviewed_cache_and_preserves_bound_marker(self):
        work, clip, video, native, marker = self.cached_item()
        before = {path: path.read_bytes() for path in work.iterdir()}
        cfg = CampaignConfig(accounts=[self.account], tasks=[], work_dir=work, content_mode='cache')

        def use_cache(active, **_kwargs):
            self.assertEqual(active.work_dir.resolve(), work.resolve())
            self.assertEqual(campaign.ego_clip_cache_state(clip, active.work_dir), 'ready')
            prepared = campaign.prepare_clip(clip, video, active.work_dir, allow_download=False)
            self.assertEqual(Path(prepared['video_path']).resolve(), native.resolve())
            log = campaign.CampaignLog(started_at='fixture', accounts=[self.account.email])
            log.status = 'done'
            return log

        active = runner.CampaignRunner()
        active.state = 'running'
        with patch.object(runner, 'run_campaign', side_effect=use_cache):
            active._run(cfg)
        self.assertEqual(active.state, 'done', active.error)
        self.assertEqual(cfg.work_dir.resolve(), work.resolve())
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.encode.assert_not_called()
        self.download.assert_not_called()
        self.assertTrue(marker.exists())

    def test_error_preserves_real_pending_journal_video_marker_and_safe_cleanup(self):
        work, clip, video, native, marker = self.cached_item()
        before = {path: path.read_bytes() for path in work.iterdir()}
        cfg = CampaignConfig(accounts=[self.account], tasks=[], work_dir=work)

        def interrupt(active, **_kwargs):
            prepared = campaign.prepare_clip(clip, video, active.work_dir, allow_download=False)
            binding = content_provenance.bind_content_delivery(prepared, 'pending-session', 'fixture-task', self.account.org_key,
                [{'index': 0, 'start_ms': 0, 'duration_ms': 60_000, 'video_path': str(native),
                  'imu_csv': prepared['imu_csv'], 'frames_csv': prepared['frames_csv'], 'sidecar_bytes': b'declared-sidecar'}])
            upload.save_sidecar(dict(session_id='pending-session', chunk_index=0, expected_chunk_count=1,
                account_email=self.account.email, org_key=self.account.org_key, task_id='fixture-task',
                log_id='pending-session_0', filename='pending-session_0.mp4',
                video_path=str(native.resolve()), local_video_path=str(native.resolve()), size_bytes=native.stat().st_size,
                state='creating', phase='queued', attempts=0, crash_resumes=0, finalized=False,
                finalize_requested=False, register_first=True,
                recorded_at=device_profile.format_recorded_at(time.time() - 120),
                campaign_context={'registry_key': 'fixture-key', 'clip_uid': 'fixture-clip',
                                  'task_id': 'fixture-task', 'content_provenance': binding}))
            raise RuntimeError('Declared offline interruption')

        active = runner.CampaignRunner()
        active.state = 'running'
        with patch.object(runner, 'run_campaign', side_effect=interrupt):
            active._run(cfg)
        self.assertEqual(active.state, 'error')
        journal_path = upload._sidecar_path('pending-session')
        journal_bytes = journal_path.read_bytes()
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(campaign.cleanup_media_cache(work, provider='ego4d')['files'], 0)
        self.assertEqual(journal_path.read_bytes(), journal_bytes)
        self.assertTrue(native.exists() and marker.exists())

        def offline_resume(**kwargs):
            self.assertEqual(Path(kwargs['video_path']).resolve(), native.resolve())
            self.assertEqual(Path(kwargs['video_path']).read_bytes(), before[native])
            raise upload.UploadError('Declared offline transport failure', transient=True)

        with patch.object(upload, 'upload_session', side_effect=offline_resume) as send:
            resumed = upload.pump_pending(SimpleNamespace(email=self.account.email), max_retries=1)
        send.assert_called_once()
        self.assertNotEqual(resumed[0]['state'], 'loss')
        self.assertTrue(native.exists() and marker.exists())


class ReconciliationProvenanceTests(WireFixtures):
    def test_invalid_current_lineage_has_no_registry_or_journal_effect(self):
        invalid = self.lineage('invalid-session')
        invalid['delivery_binding_sha256'] = 'wrong'
        self.complete_journal('invalid-session', lineage=invalid)
        self.assert_invalid_without_effects()

    def test_other_clip_invalid_lineage_blocks_all_effects_even_after_valid_session(self):
        self.complete_journal('valid-session', lineage=self.lineage('valid-session'))
        invalid = self.lineage('other-session')
        invalid['delivery_binding_sha256'] = 'wrong'
        self.complete_journal('other-session', clip='other-clip', lineage=invalid)
        self.assert_invalid_without_effects()

    def assert_invalid_without_effects(self):
        before = {path: path.read_bytes() for path in self.journals.iterdir()}
        with patch.object(campaign.sent_registry, 'recovery_reset_checker', return_value=lambda *_: False), \
             patch.object(campaign.sent_registry, 'mark_sent_many') as mark:
            with self.assertRaises(upload.UploadError) as raised:
                campaign._reconcile_uploads(upload.list_sidecars(), self.account,
                    {'clip_uid': 'fixture-clip', 'registry_key': 'fixture-key'}, 'fixture-task')
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(raised.exception.phase, 'recovery')
        mark.assert_not_called()
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_valid_binding_is_preserved_and_acknowledged(self):
        lineage = self.lineage('valid-session')
        self.complete_journal('valid-session', lineage=lineage)
        with patch.object(campaign.sent_registry, 'recovery_reset_checker', return_value=lambda *_: False), \
             patch.object(campaign.sent_registry, 'mark_sent_many') as mark:
            result = campaign._reconcile_uploads(upload.list_sidecars(), self.account,
                {'clip_uid': 'fixture-clip', 'registry_key': 'fixture-key'}, 'fixture-task')
        self.assertTrue(result['ok'])
        self.assertEqual(result['content_provenance'], lineage)
        mark.assert_called_once()
        self.assertTrue(upload.load_sidecar('valid-session')['campaign_reconciled'])


class PendingVariantCleanupTests(WireFixtures):
    def test_budget_pruning_preserves_pending_variant_and_marker_and_removes_orphan(self):
        work = config.MEDIA_DATA_DIR / 'ego4d'
        work.mkdir(parents=True)
        pending = work / 'pending_native_acc1234.mp4'
        orphan = work / 'orphan_native_acc5678.mp4'
        pending.write_bytes(b'Declared pending variant')
        orphan.write_bytes(b'Declared unreferenced old variant')
        pending_marker = campaign._account_video_ok_path(pending)
        orphan_marker = campaign._account_video_ok_path(orphan)
        pending_marker.write_bytes(b'Pending variant companion')
        orphan_marker.write_bytes(b'Unreferenced variant companion')
        upload.save_sidecar(dict(session_id='pending-variant', chunk_index=0, expected_chunk_count=1,
            account_email=self.account.email, org_key=self.account.org_key, task_id='fixture-task',
            state='creating', phase='queued', video_path=str(pending.resolve()), local_video_path=str(pending.resolve()),
            campaign_context={'registry_key': 'fixture-key', 'clip_uid': 'fixture-clip'}))
        journal = upload._sidecar_path('pending-variant')
        before = {path: path.read_bytes() for path in (pending, pending_marker, journal)}
        with patch.object(config, 'VIDEO_CACHE_GB', 1 / 1024 ** 3):
            removed, freed = campaign._enforce_account_video_cache(work)
        self.assertEqual(removed, 1)
        self.assertEqual(freed, len(b'Declared unreferenced old variant'))
        self.assertFalse(orphan.exists() or orphan_marker.exists())
        self.assertEqual({path: path.read_bytes() for path in before}, before)


class ChunkDiagnosticsTests(WireFixtures):
    def test_half_open_windows_use_same_resampling_and_observed_alignment(self):
        source = self.root / 'small.csv'
        source.write_text(HEADER + '0,1,2,3,4,5,6\n10,1,2,3,4,5,6\n20,1,2,3,4,5,6\n21,1,2,3,4,5,6\n', encoding='utf-8')
        full = ego4d.build_imu_csv(source, (0, .021), duration_ms=21)
        stats = {}
        actual = ego4d.build_imu_csv(source, (0, .021), duration_ms=21,
                                   stats=stats, stats_windows_ms=[(0, 11), (11, 21)])
        self.assertEqual(actual, full)
        self.assertEqual([row['sampleCount'] for row in stats['windows']], [6, 5])
        self.assertEqual([row['interpolatedCount'] for row in stats['windows']], [4, 4])
        self.assertEqual([row['maxAlignmentDeltaNs'] for row in stats['windows']], ['0', '1000000'])
        for window in stats['windows']:
            _, count = campaign._slice_imu_csv(full, window['start_ms'], window['end_ms'] - window['start_ms'])
            self.assertEqual(window['sampleCount'], count)

    def test_window_fallback_counts_only_its_actual_source_edge_events(self):
        source = self.root / 'edge.csv'
        rows = []
        for t in range(0, 21, 2):
            gyro = ',,' if t == 0 else '1,2,3'
            accel = ',,' if t == 20 else '4,5,6'
            rows.append(f'{t},{gyro},{accel}\n')
        source.write_text(HEADER + ''.join(rows), encoding='utf-8')
        stats = {}
        ego4d.build_imu_csv(source, (0, .02), duration_ms=20,
                          stats=stats, stats_windows_ms=[(0, 10), (10, 20)])
        self.assertEqual(stats['nearestFallbackCount'], 2)
        self.assertEqual([row['nearestFallbackCount'] for row in stats['windows']], [1, 0])
        self.assertEqual([row['interpolatedCount'] for row in stats['windows']], [1, 0])

    def test_actual_upload_policy_emits_distinct_chunk_counters_without_mutating_item(self):
        media = self.root / 'prepared.mp4'
        media.write_bytes(b'Declared inert prepared media')
        source = self.root / 'imu.csv'
        times = [*range(0, 60_000, 10), *range(60_000, 120_001, 20)]
        source.write_text(HEADER + ''.join(f'{t},1,2,3,4,5,6\n' for t in times), encoding='utf-8')
        stats = {}
        imu = ego4d.build_imu_csv(source, (0, 120), duration_ms=120_000, stats=stats)
        item = content_provenance.prepare_content_provenance(media, source, media, imu,
            'i,ptsNs,dtNs,tNs,key\n0,0,0,0,1\n', clip_uid='fixture-clip', parent_video_uid='fixture-parent',
            media_uid='fixture-media', window_s=(0, 120), media_offset_s=0, normalization_start_s=0, selection_evidence=None)
        item.update(source='ego4d', imu_real=True, clip_uid='fixture-clip', video_path=str(media),
                    duration_ms=120_000, n_samples=stats['sampleCount'], imu_diagnostics=stats,
                    probe={'duration_ms': 120_000, 'fps': 30, 'width': 1440, 'height': 1080},
                    _content_candidate={'clip_uid': 'fixture-clip'}, registry_key='fixture-key')
        before = copy.deepcopy(item)
        session = SimpleNamespace(_live=True, email=self.account.email, warmup=lambda: None,
            recording_policy=SimpleNamespace(limits=lambda: dict(min_duration_ms=60_000, max_duration_ms=60_000)))
        profile = SimpleNamespace(frames_gop=30, uptime_ns_at=lambda _wall: UPTIME_NS, calib={},
                                  sidecar_device_meta=lambda: None, sidecar_platform_meta=lambda: None)

        def cut(_source, destination, start, duration):
            destination.write_bytes(f'Declared chunk {start} {duration}'.encode())
            return destination

        def frames(_path, **kwargs):
            self.assertTrue(kwargs['require_measured_pts'])
            return sidecar.build_frames_csv(kwargs['duration_ms'], kwargs['fps'], offset_ns=kwargs['offset_ns'])

        with patch.object(campaign.ego4d, 'revalidate_selection_evidence', return_value={'schema': 1}), \
             patch.object(campaign.org_policy, 'account_kind', return_value='other'), \
             patch.object(campaign.device_profile, 'get_profile', return_value=profile), \
             patch.object(campaign, '_new_identity', return_value=('chunk-session', 'chunk-session_0', '2026-10-04T12:00:00.000Z')), \
             patch.object(campaign, '_cut_video_chunk', side_effect=cut), \
             patch.object(campaign, 'probe_video', return_value={'duration_ms': 60_000, 'fps': 30, 'width': 1440, 'height': 1080}), \
             patch.object(campaign, 'build_frames_csv_from_video', side_effect=frames), \
             patch.object(campaign, 'upload_session', return_value=SimpleNamespace(session_id='chunk-session',
                finalized=True, finalize_status=204, chunks=[SimpleNamespace(state='done', error=None,
                upload_id=f'fixture-{i}', evaluate_result={}) for i in range(2)])) as send:
            result = campaign.upload_to_account(item, self.account, 'fixture-task', 30, True, True,
                                                session=session, recover_pending=False)
        self.assertTrue(result['ok'], result.get('error'))
        self.assertEqual(item, before)
        send.assert_called_once()
        blobs = send.call_args.kwargs['sidecar_data']
        self.assertEqual(len(blobs), 2)
        interpolated = []
        for index, blob in enumerate(blobs):
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                metadata = json.loads(archive.read(f'chunk-session_{index}.metadata.json'))
                count = len(list(csv.DictReader(io.StringIO(archive.read(f'chunk-session_{index}.imu.csv').decode()))))
            diag = metadata['imuDiagnostics']
            self.assertEqual(count, 30_000)
            self.assertEqual(diag['sampleCount'], count)
            self.assertEqual(diag['nearestFallbackCount'], 0)
            self.assertEqual(diag['maxAlignmentDeltaNs'], '0')
            self.assertLessEqual(diag['interpolatedCount'], count)
            interpolated.append(diag['interpolatedCount'])
        self.assertEqual(interpolated, [24_000, 27_000])


if __name__ == '__main__':
    unittest.main()
