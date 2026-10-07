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

PARENT = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'


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
