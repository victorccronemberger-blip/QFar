"""Inert current catalogs and media bytes; no physical/provider/backend proof."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from moneymin import campaign, content_provenance as cp, ego4d
from moneymin.campaign_types import AccountSpec


class ContentTaskBinding(unittest.TestCase):
    def test_semantic_task_name_is_revalidated_before_unknown_receiver_gate(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            root = Path(folder)
            task = 'Folding Clothes or Putting Them on Hangers'
            video = {'video_uid': 'parent', 'duration_sec': 600, 'has_imu': True,
                     'scenarios': ['Cleaning / laundry'], 's3_path': 's3://fixture/parent.mp4',
                     'imu_metadata': {'s3_path': 's3://fixture/parent.csv', 'component_metadata': [
                         {'canonical_video_start_ms': 0, 'canonical_video_end_ms': 600000}]}}
            meta, clips = root / 'ego4d.json', root / 'clips.csv'
            meta.write_text(json.dumps({'videos': [video]}), encoding='utf-8')
            clips.write_text('exported_clip_uid,parent_video_uid,parent_start_sec,parent_end_sec,s3_path\n'
                             'c0,parent,0,600,s3://fixture/c0.mp4\n', encoding='utf-8')
            (root / 'clip_narrations.json').write_text(json.dumps({'c0': '#C C folds the shirt'}), encoding='utf-8')
            (root / 'timed_narrations.jsonl').write_text(json.dumps({'video_uid': 'parent',
                'events': [[t, '#C C folds the shirt'] for t in range(0, 601, 5)]}) + '\n', encoding='utf-8')
            stack.enter_context(patch.object(ego4d, 'EGO4D_DIR', root))
            stack.enter_context(patch.object(ego4d, 'sync_meta', return_value=(meta, clips)))
            stack.enter_context(patch.object(campaign, '_rank_seed_path', return_value=root / 'absent-seed.gz'))
            for func in (ego4d._catalog, ego4d._action_index_cached, ego4d._load_timed_narrations_cached,
                         campaign._load_rank_seed, campaign._rank_cache_stamp):
                if hasattr(func, 'cache_clear'):
                    func.cache_clear(); stack.callback(func.cache_clear)
            selected = ego4d.attach_selection_evidence(ego4d.list_clips(min_dur_s=60, max_dur_s=1800)[0], task)
            source, prepared, imu = root / 'source.mp4', root / 'prepared.mp4', root / 'source.csv'
            source.write_bytes(b'declared-original-media'); prepared.write_bytes(b'declared-derived-media')
            imu.write_text('canonical_timestamp_ms,gyro_x,gyro_y,gyro_z,accl_x,accl_y,accl_z\n'
                           '0,1,2,3,4,5,6\n600000,1,2,3,4,5,6\n', encoding='utf-8')
            item = cp.prepare_content_provenance(source, imu, prepared,
                't,ax,ay,az,wx,wy,wz\n0,4,5,6,1,2,3\n600000000000,4,5,6,1,2,3\n',
                'i,ptsNs,dtNs,tNs,key\n0,0,0,0,1\n', clip_uid='c0', parent_video_uid='parent',
                media_uid='c0', window_s=(0, 600), media_offset_s=0, normalization_start_s=None,
                selection_evidence=selected['selection_evidence'])
            item.update(source='ego4d', imu_real=True, clip_uid='c0', video_path=str(prepared),
                        duration_ms=600000, _content_candidate=selected,
                        task_name='Translated display label', task_scenario='Cleaning / laundry',
                        task_name_authoritative=task, registry_key='fixture-registry')
            real = ego4d.revalidate_selection_evidence
            with patch.object(ego4d, 'revalidate_selection_evidence', wraps=real) as validator, \
                 patch.object(campaign.Session, 'from_email') as auth, \
                 patch.object(campaign, 'upload_session') as upload, \
                 patch.object(campaign, 'pump_pending') as recovery:
                result = campaign.upload_to_account(item, AccountSpec('fixture@example.invalid', 'fixture-org'),
                    'fixture-task-id', 30, True, True, recover_pending=False)
            self.assertFalse(result['ok']); self.assertFalse(result['retryable'])
            self.assertEqual(result['error'], cp.DATASET_POLICY_ERROR)
            self.assertEqual(validator.call_count, 1)
            self.assertEqual(validator.call_args.kwargs['task_name'], task)
            self.assertEqual(result['selection_evidence']['task']['name'], task)
            self.assertEqual(result['selection_evidence']['task']['id'], 'fixture-task-id')
            self.assertEqual(result['selection_evidence']['task']['registry_key'], 'fixture-registry')
            auth.assert_not_called(); upload.assert_not_called(); recovery.assert_not_called()


if __name__ == '__main__':
    unittest.main()
