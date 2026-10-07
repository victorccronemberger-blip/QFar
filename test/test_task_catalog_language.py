"""Canonical activity keys remain stable when the API translates its catalog."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from moneymin import campaign, config, ego4d, minute_api, task_catalog


class TaskCatalogLanguageTests(unittest.TestCase):
    def test_translated_api_catalog_still_selects_current_dataset_content(self):
        canonical = {'id': 'fixture-shopping', 'name': 'Shopping',
                     'description': 'Fixture shopping activity',
                     'categories': [{'slug': 'errands', 'label': 'Errands'}]}
        for language, translated in (('pt-BR,pt;q=0.9', 'Fazer Compras'),
                                     ('es-ES,es;q=0.9', 'Hacer compras'),
                                     ('en-US,en;q=0.9', 'Shopping')):
            with self.subTest(language=language), tempfile.TemporaryDirectory() as folder, \
                 patch.object(config, 'ACCEPT_LANGUAGE', language), \
                 patch.object(config, 'DATA_DIR', Path(folder)), \
                 patch.object(config, 'MEDIA_DATA_DIR', Path(folder)), \
                 patch.object(ego4d, 'EGO4D_DIR', Path(folder) / 'ego4d'), \
                 patch.object(campaign, '_RANK_INPUT_SIGNATURE', None):
                library = Path(folder) / 'ego4d'
                library.mkdir()
                (library / 'ego4d.json').write_text(json.dumps({'videos': [{
                    'video_uid': 'shopping-parent', 'has_imu': True, 'duration_sec': 600,
                    'scenarios': ['Grocery shopping indoors'], 's3_path': 's3://fixture/shopping.mp4'}]}), 'utf8')
                (library / 'clips.csv').write_text(
                    'exported_clip_uid,parent_video_uid,parent_start_sec,parent_end_sec,s3_path\n'
                    'shopping-clip,shopping-parent,0,600,s3://fixture/shopping-clip.mp4\n', 'utf8')
                (library / 'clip_narrations.json').write_text(json.dumps({
                    'shopping-clip': '#C C picks groceries from the store shelf'}), 'utf8')
                (library / 'timed_narrations.jsonl').write_text(json.dumps({
                    'video_uid': 'shopping-parent', 'events': [[t, '#C C picks groceries from the store shelf']
                                                              for t in range(0, 601, 5)]}), 'utf8')
                session = minute_api.Session.__new__(minute_api.Session)
                # Simulate the observed API: translated by langCode unless
                # canonical English is explicitly requested. No auth/network.
                def catalog(path):
                    english = path.endswith('?langCode=en')
                    row = canonical if english else {**canonical, 'name': translated}
                    return 200, json.dumps({'tasks': [row]})
                session.get = Mock(side_effect=catalog)
                tasks = session.all_tasks('fixture-org')
                session.get.assert_called_once_with('/api/v1/orgs/fixture-org/tasks?langCode=en')
                self.assertEqual(tasks, [canonical])
                rows = campaign.available_tasks('fixture@example.invalid', 'fixture-org',
                    remote_tasks=tasks, dataset_provider='ego4d', content_mode='dataset',
                    min_dur_s=300, max_dur_s=1800, include_unavailable=True)
                self.assertEqual(rows[0]['id'], canonical['id'])
                self.assertTrue(rows[0]['available_for_duration'])
                self.assertGreater(rows[0]['clip_count'], 0)
                self.assertEqual(rows[0]['name_pt'], 'Fazer compras')

    def test_unsupported_canonical_activity_does_not_gain_content(self):
        session = minute_api.Session.__new__(minute_api.Session)
        session.get = Mock(return_value=(200, json.dumps([{
            'id': 'fixture-unsupported', 'name': 'A future task'}])))
        tasks = session.all_tasks('fixture-org')
        with patch.object(campaign, '_compatible_task_clips') as clips:
            rows = campaign.available_tasks('fixture@example.invalid', 'fixture-org',
                remote_tasks=tasks, dataset_provider='ego4d', content_mode='dataset',
                include_unavailable=True)
        self.assertEqual(rows[0]['clip_count'], 0)
        self.assertFalse(rows[0]['mapping_supported'])
        clips.assert_not_called()

    def test_current_library_names_have_portuguese_presentation(self):
        for name in task_catalog.library_task_names():
            with self.subTest(name=name):
                self.assertIn(name, task_catalog.TASK_NAME_PT)
                self.assertNotEqual(task_catalog.TASK_NAME_PT[name], name)
        self.assertEqual(task_catalog.CATEGORY_PT['interactions'], 'Interações')

    def test_presentation_preserves_people_and_appliance_requirements(self):
        self.assertIn('2 pessoas', task_catalog.TASK_NAME_PT[
            'Move furniture with someone (2+ people required)'])
        self.assertIn('outra pessoa', task_catalog.TASK_NAME_PT['Serve food'])
        self.assertEqual(task_catalog.TASK_NAME_PT['Using the Dishwasher'],
                         'Usar a máquina de lavar louça')


if __name__ == '__main__':
    unittest.main()
