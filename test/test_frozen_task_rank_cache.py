"""Installed updates invalidate ranked content without source files in PYZ."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from moneymin import campaign, ego4d, task_matching


class FrozenTaskRankCacheTests(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for target, name, value in (
            (campaign.config, 'DATA_DIR', self.root / 'data'),
            (campaign.config, 'MEDIA_DATA_DIR', self.root / 'library'),
            (ego4d, 'EGO4D_DIR', self.root / 'library' / 'ego4d'),
            (ego4d, '__file__', str(self.root / 'pyz' / 'ego4d.py')),
            (task_matching, '__file__', str(self.root / 'pyz' / 'task_matching.py')),
        ):
            pending = patch.object(target, name, value)
            pending.start()
            self.addCleanup(pending.stop)

    def test_missing_sources_do_not_hide_rule_changes(self):
        before = campaign._rank_cache_stamp()
        self.assertTrue(any(row[0] == 'ego4d.py' and row[2] == 'missing' for row in before))
        name = 'Using the Dishwasher'
        changed = dict(task_matching.TASK_RULES)
        changed[name] = replace(changed[name], required_span_patterns=('new verified phase',))
        with patch.object(task_matching, 'TASK_RULES', changed):
            self.assertNotEqual(before, campaign._rank_cache_stamp())

    def test_alias_changes_invalidate_stamp_and_dictionary_order_does_not(self):
        before = campaign._rank_cache_stamp()
        with patch.object(task_matching, 'TASK_RULES', dict(reversed(list(task_matching.TASK_RULES.items())))), \
             patch.object(task_matching, 'TASK_ALIASES', dict(reversed(list(task_matching.TASK_ALIASES.items())))):
            self.assertEqual(before, campaign._rank_cache_stamp())
        with patch.object(task_matching, 'TASK_ALIASES', dict(task_matching.TASK_ALIASES, **{'Future spelling': 'Using the Dishwasher'})):
            self.assertNotEqual(before, campaign._rank_cache_stamp())

    def test_saved_old_index_is_rejected_and_rebuilt_after_installed_rule_update(self):
        path = campaign._rank_cache_path()
        campaign._save_rank_cache({'Using the Dishwasher': ()}, path, narration_scan=False)
        old_bytes = path.read_bytes()
        self.assertIsNotNone(campaign._load_rank_cache(path))
        changed = dict(task_matching.TASK_RULES)
        changed['Using the Dishwasher'] = replace(changed['Using the Dishwasher'], required_span_patterns=('new verified phase',))
        with patch.object(task_matching, 'TASK_RULES', changed):
            self.assertIsNone(campaign._load_rank_cache(path))
            # Discarding an obsolete index is read-only until indexing publishes
            # its replacement; this does not erase footage or journals.
            self.assertEqual(path.read_bytes(), old_bytes)
            campaign._save_rank_cache({'Using the Dishwasher': (), 'Brew Coffee or Tea': ()}, path,
                                      narration_scan=False)
            self.assertIn('Brew Coffee or Tea', campaign._load_rank_cache(path))

    def test_algorithm_change_outside_rules_invalidates_existing_index(self):
        campaign._save_rank_cache({}, narration_scan=False)
        with patch.object(ego4d, '_SELECTION_VERSION', 'future-context-algorithm'):
            self.assertIsNone(campaign._load_rank_cache())
