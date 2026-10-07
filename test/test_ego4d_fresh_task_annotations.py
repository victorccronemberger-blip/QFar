"""Fresh licensed catalogs acquire the evidence their effect gates require."""
import io
import json
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from moneymin import campaign, config, ego4d
from moneymin.campaign_types import CampaignConfig, TaskSpec

PARENT = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
_REAL_SYNC_META = ego4d.sync_meta


class FreshTaskAnnotationsTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(config, 'MEDIA_DATA_DIR', self.root))
        self.stack.enter_context(patch.object(ego4d, 'EGO4D_DIR', self.root / 'ego4d'))
        ego4d.EGO4D_DIR.mkdir()
        meta, clips = ego4d.EGO4D_DIR / 'ego4d.json', ego4d.EGO4D_DIR / 'clips.csv'
        meta.write_text(json.dumps({'videos': [{'video_uid': PARENT, 'has_imu': True,
            'duration_sec': 700, 'scenarios': ['Cleaning / laundry'],
            'imu_metadata': {'s3_path': 's3://fixture/imu.csv', 'component_metadata': [
                {'canonical_video_start_ms': 0, 'canonical_video_end_ms': 700000}]},
            's3_path': 's3://fixture/parent.mp4'}]}), encoding='utf-8')
        clips.write_text('exported_clip_uid,parent_video_uid,parent_start_sec,parent_end_sec,s3_path\n', encoding='utf-8')
        self.sync = self.stack.enter_context(patch.object(ego4d, 'sync_meta', return_value=(meta, clips)))
        self.resource = Mock()
        self.stack.enter_context(patch.object(ego4d, '_s3', return_value=self.resource))
        self.task = 'Folding Clothes or Putting Them on Hangers'
        self.payload = json.dumps({'videos': {
            PARENT: {'narrations': [{'time': t, 'text': '#C C folds the shirt'}
                                     for t in range(0, 601, 5)]},
            'without-imu': {'narrations': [{'time': 0, 'text': '#C cooks'}]},
        }}).encode()

    def body(self, data=None):
        body = io.BytesIO(self.payload if data is None else data)
        self.resource.meta.client.get_object.return_value = {'Body': body}
        return body

    def test_fresh_suggestion_becomes_current_proof_before_any_media_download(self):
        clip = ego4d._span_record(ego4d._cat().videos[PARENT], {'start': 0, 'end': 600})
        old = ego4d.attach_selection_evidence(clip, self.task)
        with self.assertRaises(ValueError):
            ego4d.revalidate_selection_evidence(old)
        body = self.body()
        with patch.object(ego4d, '_download_to', side_effect=AssertionError('No video downloads')):
            path = ego4d.sync_task_annotations()
            rows = ego4d.list_task_spans(self.task)
            self.assertTrue(rows)
            for row in rows:
                self.assertEqual(ego4d.revalidate_selection_evidence(row)['justification']['status'],
                                 'locally_revalidated')
            with self.assertRaises(ValueError):
                ego4d.revalidate_selection_evidence(old)  # stale suggestions are never re-approved
        self.assertTrue(body.closed)
        self.assertEqual(json.loads(path.read_text())['video_uid'], PARENT)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()),
                         ['clips.csv', 'ego4d.json', 'timed_narrations.jsonl'])
        self.resource.meta.client.get_object.assert_called_once_with(
            Bucket=ego4d.MANIFEST_BUCKET, Key=ego4d.NARRATIONS_KEY)

    def test_malformed_stream_is_closed_and_partial_annotation_is_removed(self):
        body = self.body(self.payload[:-2])
        with self.assertRaises(ValueError):
            ego4d.sync_task_annotations()
        self.assertTrue(body.closed)
        self.assertFalse(ego4d.has_timed_narrations())
        self.assertFalse(list(ego4d.EGO4D_DIR.glob('.*.refresh')))

    def test_existing_annotations_are_preserved_without_any_remote_read(self):
        target = ego4d.timed_narrations_path()
        original = b'{"video_uid":"existing","events":[[0,"#C folds"]]}\n'
        target.write_bytes(original)
        self.assertEqual(ego4d.sync_task_annotations(), target)
        self.assertEqual(target.read_bytes(), original)
        self.resource.meta.client.get_object.assert_not_called()

    def test_parallel_bootstrap_downloads_once(self):
        self.body()
        errors = []
        def work():
            try:
                ego4d.sync_task_annotations()
            except Exception as exc:
                errors.append(exc)
        workers = [threading.Thread(target=work) for _ in range(3)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.resource.meta.client.get_object.assert_called_once()

    def test_browsing_without_license_does_not_start_a_download(self):
        with patch.object(ego4d, '_aws_creds', side_effect=RuntimeError('missing')):
            ego4d.ensure_task_annotations()
        self.sync.assert_not_called()
        self.resource.meta.client.get_object.assert_not_called()

    def test_duration_selection_bootstraps_before_reading_annotation_presence(self):
        self.body()
        with patch.object(ego4d, '_aws_creds', return_value=('private', 'private', None)), \
             patch.object(ego4d, '_valid_metadata', return_value=True), \
             patch.object(campaign, '_refresh_rank_inputs'), \
             patch.object(campaign, '_duration_ranked_pools', return_value={}) as ranked:
            campaign._compatible_task_clips(self.task, 'ego4d', min_dur_s=300, max_dur_s=1800)
        self.assertTrue(ego4d.has_timed_narrations())
        ranked.assert_called_once_with(300, 1800)

    def test_unconfigured_public_install_never_queues_a_portable_suggestion(self):
        clip = ego4d._span_record(ego4d._cat().videos[PARENT], {'start': 0, 'end': 600})
        suggested = ego4d.attach_selection_evidence(clip, self.task)
        self.assertFalse(campaign._prepare_queue_accepts(suggested, self.task))
        with patch.object(ego4d, '_aws_creds', side_effect=RuntimeError('missing')), \
             patch.object(campaign, '_compatible_task_clips', return_value=(suggested,)):
            rows = campaign.available_tasks('fixture@example.invalid', 'fixture',
                remote_tasks=[{'id': 'fold', 'name': self.task}], dataset_provider='ego4d',
                content_mode='dataset', include_unavailable=True)
        self.assertEqual(rows[0]['clip_count'], 0)
        self.assertFalse(rows[0]['available_for_duration'])
        self.assertIn('Integrações', rows[0]['unavailable_reason'])
        self.resource.meta.client.get_object.assert_not_called()

    def test_public_install_bootstraps_from_empty_root_and_selects_current_proof(self):
        # Provider responses are inert, not a copied local library or private ZIP.
        meta, clips = self.sync.return_value
        provider_meta = meta.read_bytes()
        provider_clips = (clips.read_text() +
            f'fixture-export,{PARENT},0,700,s3://fixture/export.mp4\n').encode()
        meta.unlink()
        clips.unlink()
        self.body()
        def acquire(_bucket, key, destination):
            if key == ego4d.METADATA_KEY:
                destination.write_bytes(provider_meta)
            elif key == ego4d.CLIPS_MANIFEST_KEY:
                destination.write_bytes(provider_clips)
            else:
                raise AssertionError('Video or IMU must not be downloaded')
        self.stack.enter_context(patch.object(config, 'DATA_DIR', self.root / 'customer-state'))
        self.stack.enter_context(patch.object(campaign, '_RANK_INPUT_SIGNATURE', None))
        for cached in (campaign._task_candidates, campaign._rank_cache_stamp,
                       campaign._ranked_pools_cached, campaign._duration_ranked_pools):
            cached.cache_clear()
            self.addCleanup(cached.cache_clear)
        task = TaskSpec('fold', 'Cleaning / laundry', 300, 1800, task_name=self.task)
        cfg = CampaignConfig(accounts=[], tasks=[task], work_dir=self.root / 'ego4d',
                             dataset_provider='ego4d', content_mode='dataset')
        # Restore the real normal downloader captured before setUp's patch.
        with patch.object(ego4d, 'sync_meta', _REAL_SYNC_META), \
             patch.object(ego4d, '_download_to', side_effect=acquire), \
             patch.object(ego4d, '_aws_creds', return_value=('fixture', 'fixture', None)), \
             patch.object(ego4d, '_valid_metadata', return_value=True), \
             patch.object(campaign, '_load_rank_seed', return_value={}):
            rows = campaign.available_tasks('fixture@example.invalid', 'fixture',
                remote_tasks=[{'id': 'fold', 'name': self.task}], dataset_provider='ego4d',
                content_mode='dataset', min_dur_s=300, max_dur_s=1800)
            selected = campaign.automatic_candidates(task, cfg)
            repeated = campaign.automatic_candidates(task, cfg)
        self.assertTrue(rows[0]['available_for_duration'])
        self.assertTrue(selected)
        self.assertEqual([c['clip_uid'] for c in selected], [c['clip_uid'] for c in repeated])
        for clip in selected:
            self.assertEqual(ego4d.revalidate_selection_evidence(clip)['justification']['status'],
                             'locally_revalidated')
        self.resource.meta.client.get_object.assert_called_once()
        self.assertFalse(list(self.root.rglob('*.mp4')))
        self.assertFalse(list(self.root.rglob('*.zip')))

    def test_corrupt_nonempty_annotation_recovers_within_the_same_selection_operation(self):
        target = ego4d.timed_narrations_path()
        target.write_text('null\n[]\n{"video_uid":"broken","events":42}\n', 'utf8')
        self.body()
        with ego4d.selection_operation():
            self.assertEqual(ego4d.load_timed_narrations(), {})
            ego4d.sync_task_annotations()
            self.assertIn(PARENT, ego4d.load_timed_narrations())
            for clip in ego4d.list_task_spans(self.task):
                ego4d.revalidate_selection_evidence(clip)
        self.resource.meta.client.get_object.assert_called_once()

    def test_failed_repair_preserves_previous_nonempty_file(self):
        target = ego4d.timed_narrations_path()
        original = b'{"incomplete":'
        target.write_bytes(original)
        self.body(self.payload[:-2])
        with self.assertRaises(ValueError):
            ego4d.sync_task_annotations()
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse(list(target.parent.glob('.*.refresh')))

    def test_invalid_metadata_is_explicit_instead_of_admitting_the_seed(self):
        with patch.object(ego4d, '_aws_creds', return_value=('fixture', 'fixture', None)), \
             patch.object(ego4d, '_valid_metadata', return_value=False):
            with self.assertRaisesRegex(ValueError, 'Catálogo Ego4D inválido'):
                ego4d.ensure_task_annotations()
        self.resource.meta.client.get_object.assert_not_called()
