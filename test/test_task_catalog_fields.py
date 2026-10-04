"""Malformed remote task fields must produce a private domain error before selection."""
import copy
import json
import unittest
from unittest.mock import Mock, patch

from moneymin import campaign, minute_api


class TaskCatalogFieldsTests(unittest.TestCase):
    def catalog(self, changes):
        return [{'id': 'fixture-task', 'name': 'Furniture Assembly',
                 'description': 'Fixture activity', 'categories': [{'slug': 'home', 'label': 'Fixture'}], **changes}]

    def malformed(self):
        return [{'name': 42}, {'name': ['private-catalog-canary']},
                {'categories': [42]}, {'categories': {}}, {'categories': 'private-catalog-canary'},
                {'categories': [{'slug': []}]}, {'categories': [{'label': {}}]},
                {'description': {'private': 'private-catalog-canary'}}]

    def test_available_tasks_validates_every_row_before_media_selection(self):
        for changes in self.malformed():
            with self.subTest(changes=changes), patch.object(campaign, '_compatible_task_clips') as media:
                rows = self.catalog(changes)
                before = copy.deepcopy(rows)
                with self.assertRaises(minute_api.AuthError) as raised:
                    campaign.available_tasks('fixture@example.invalid', 'fixture-org', remote_tasks=rows)
                self.assertEqual(raised.exception.account_issue_code, 'invalid_response')
                self.assertNotIn('private-catalog-canary', str(raised.exception))
                media.assert_not_called()
                self.assertEqual(rows, before)

    def test_session_catalog_parser_rejects_malformed_task_fields(self):
        session = minute_api.Session.__new__(minute_api.Session)
        for changes in self.malformed():
            session.get = Mock(return_value=(200, json.dumps({'tasks': self.catalog(changes)})))
            with self.subTest(changes=changes), self.assertRaises(minute_api.AuthError):
                session.all_tasks('fixture-org')

    def test_optional_null_and_well_formed_categories_remain_supported(self):
        session = minute_api.Session.__new__(minute_api.Session)
        for categories in (None, [], [{'slug': 'home', 'label': 'Fixture'}]):
            rows = self.catalog({'categories': categories})
            session.get = Mock(return_value=(200, json.dumps(rows)))
            with self.subTest(categories=categories):
                self.assertEqual(session.all_tasks('fixture-org'), rows)
                with patch.object(campaign, '_compatible_task_clips', return_value=[]):
                    selected = campaign.available_tasks('fixture@example.invalid', 'fixture-org', remote_tasks=rows, include_unavailable=True)
                self.assertEqual(selected[0]['id'], 'fixture-task')
