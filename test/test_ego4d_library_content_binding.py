"""Offline catalog content identity; no provider, accounts or physical capture."""
import csv
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from moneymin import ego4d_library


class Ego4DLibraryContentBindingTests(unittest.TestCase):
    UID = '11111111-1111-4111-8111-111111111111'
    CLIP = '22222222-2222-4222-8222-222222222222'

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ego-library-content-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.index = self.root / 'library.sqlite3'
        (self.root / 'ego4d.json').write_text(json.dumps({
            'version': 'inert-local-fixture', 'videos': [
                {'video_uid': self.UID, 'duration_sec': 120,
                 'scenarios': ['Cooking'], 'has_imu': True}]}), encoding='utf-8')
        with (self.root / 'clips.csv').open('w', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream, lineterminator='\n')
            writer.writerow(['exported_clip_uid', 'parent_video_uid', 'parent_start_sec', 'parent_end_sec'])
            writer.writerow([self.CLIP, self.UID, 0, 60])
        (self.root / 'timed_narrations.jsonl').write_text(json.dumps({
            'video_uid': self.UID, 'events': [[20, '#C stirs']]}), encoding='utf-8')
        ego4d_library.index_library(self.root, self.index)

    def source_bytes(self):
        return {name: (self.root / name).read_bytes()
                for name in ('ego4d.json', 'clips.csv', 'timed_narrations.jsonl')}

    def mutate_same_stat(self, name):
        path = self.root / name
        prior = path.read_bytes()
        stamp = path.stat()
        replacements = {
            'ego4d.json': (b'Cooking', b'Walking'),
            'clips.csv': (b',60\n', b',70\n'),
            'timed_narrations.jsonl': (b'stirs', b'walks'),
        }
        old, new = replacements[name]
        self.assertIn(old, prior)
        updated = prior.replace(old, new)
        self.assertEqual(len(updated), len(prior))
        path.write_bytes(updated)
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertEqual((path.stat().st_size, path.stat().st_mtime_ns),
                         (stamp.st_size, stamp.st_mtime_ns))
        self.assertNotEqual(hashlib.sha256(prior).digest(), hashlib.sha256(updated).digest())

    def assert_no_owned_temporary(self):
        self.assertEqual([p.name for p in self.root.iterdir()
                          if p.name.endswith('.tmp') or p.name.endswith('.inputs')], [])

    def test_healthy_index_hashes_match_actual_parsed_sources(self):
        before = self.source_bytes()
        report = ego4d_library.index_library(self.root, self.index)
        self.assertEqual(report['videos'], 1)
        self.assertEqual(report['clips'], 1)
        self.assertEqual(report['annotations'], 1)
        with closing(sqlite3.connect(self.index)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)
            stored = dict(db.execute('SELECT name,sha256 FROM source_file'))
            self.assertEqual(db.execute('SELECT duration_s FROM clip').fetchone()[0], 60)
        self.assertEqual(stored, {name: hashlib.sha256(raw).hexdigest() for name, raw in before.items()})
        self.assertEqual(self.source_bytes(), before)
        self.assertEqual(ego4d_library.library_summary(self.index, self.root)['state'], 'ready')
        self.assertEqual(ego4d_library.browse_library(self.index, 'Cooking')['total'], 1)
        self.assert_no_owned_temporary()

    def test_same_size_and_mtime_content_changes_are_stale_for_all_sources(self):
        for name in ('ego4d.json', 'clips.csv', 'timed_narrations.jsonl'):
            with self.subTest(source=name):
                original = self.source_bytes()
                self.mutate_same_stat(name)
                state = ego4d_library.library_summary(self.index, self.root)
                self.assertEqual(state['state'], 'stale')
                self.assertIs(state['needs_index'], True)
                for source, raw in original.items():
                    (self.root / source).write_bytes(raw)
                ego4d_library.index_library(self.root, self.index)

    def test_same_stat_change_during_index_preserves_previous_index(self):
        for name in ('ego4d.json', 'clips.csv', 'timed_narrations.jsonl'):
            with self.subTest(source=name):
                ego4d_library.index_library(self.root, self.index)
                prior = self.index.read_bytes()
                fired = []

                def change_after_video_clip_rows(message):
                    if message.startswith('Indexando') and not fired:
                        fired.append(True)
                        self.mutate_same_stat(name)

                with self.assertRaises(ValueError):
                    ego4d_library.index_library(self.root, self.index, progress=change_after_video_clip_rows)
                self.assertEqual(fired, [True])
                self.assertEqual(self.index.read_bytes(), prior)
                self.assertEqual(ego4d_library.library_summary(self.index, self.root)['state'], 'stale')
                self.assert_no_owned_temporary()
                ego4d_library.index_library(self.root, self.index)

    def test_bad_json_rebuild_preserves_previous_index_and_inputs(self):
        prior = self.index.read_bytes()
        path = self.root / 'ego4d.json'
        path.write_bytes(b'{broken fixture')
        original = self.source_bytes()
        with self.assertRaises(ValueError):
            ego4d_library.index_library(self.root, self.index)
        self.assertEqual(self.index.read_bytes(), prior)
        self.assertEqual(self.source_bytes(), original)
        self.assert_no_owned_temporary()

    def test_optional_source_presence_change_during_index_is_rejected(self):
        (self.root / 'timed_narrations.jsonl').unlink()
        ego4d_library.index_library(self.root, self.index)
        prior = self.index.read_bytes()

        def add_source(message):
            if message.startswith('Indexando'):
                (self.root / 'timed_narrations.jsonl').write_text(
                    json.dumps({'video_uid': self.UID, 'events': [[20, 'LABORATORY']]}), encoding='utf-8')

        with self.assertRaises(ValueError):
            ego4d_library.index_library(self.root, self.index, progress=add_source)
        self.assertEqual(self.index.read_bytes(), prior)
        self.assert_no_owned_temporary()

    def test_original_sensor_presence_not_snapshot_path_or_verification(self):
        sensor = self.root / (self.UID + '_imu.csv')
        sensor.write_bytes(b'INERT PRESENCE FIXTURE NOT SENSOR PROOF')
        original = sensor.read_bytes()
        report = ego4d_library.index_library(self.root, self.index)
        self.assertEqual(report['local_sensor_files'], 1)
        summary = ego4d_library.library_summary(self.index, self.root)
        self.assertEqual(summary['local_sensor_files'], 1)
        self.assertEqual(summary['sensor_verification'], 'not_implied_by_presence')
        self.assertTrue(ego4d_library.browse_library(self.index, 'Cooking')['items'][0]['imu_local'])
        self.assertEqual(sensor.read_bytes(), original)
        self.assert_no_owned_temporary()

    def test_missing_required_source_keeps_previous_index(self):
        prior = self.index.read_bytes()
        (self.root / 'clips.csv').unlink()
        with self.assertRaises((ValueError, OSError)):
            ego4d_library.index_library(self.root, self.index)
        self.assertEqual(self.index.read_bytes(), prior)
        self.assert_no_owned_temporary()

    def test_snapshot_built_index_emits_exact_binding_revision(self):
        with closing(sqlite3.connect(self.index)) as db:
            report = json.loads(db.execute('SELECT json FROM index_report').fetchone()[0])
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)
        self.assertIs(type(report.get('source_binding_revision')), int)
        self.assertEqual(report['source_binding_revision'], 1)
        self.assertEqual(ego4d_library.library_summary(self.index, self.root)['state'], 'ready')

    def test_legacy_or_unknown_binding_revision_requires_reindex(self):
        with closing(sqlite3.connect(self.index)) as db:
            original = json.loads(db.execute('SELECT json FROM index_report').fetchone()[0])
        for label, value in [('missing', None), ('null', None), ('zero', 0),
                             ('future', 2), ('boolean', True), ('text', '1')]:
            with self.subTest(binding_revision=label):
                report = dict(original)
                report.pop('source_binding_revision', None)
                if label != 'missing':
                    report['source_binding_revision'] = value
                with closing(sqlite3.connect(self.index)) as db:
                    db.execute('UPDATE index_report SET json=?', (json.dumps(report),))
                    db.commit()
                legacy = self.index.read_bytes()
                state = ego4d_library.library_summary(self.index, self.root)
                self.assertEqual(state['state'], 'outdated')
                self.assertIs(state['needs_index'], True)
                path = self.root / 'ego4d.json'
                raw = path.read_bytes()
                path.write_bytes(b'{bad rebuild fixture')
                with self.assertRaises(ValueError):
                    ego4d_library.index_library(self.root, self.index)
                self.assertEqual(self.index.read_bytes(), legacy)
                path.write_bytes(raw)
                self.assert_no_owned_temporary()
        ego4d_library.index_library(self.root, self.index)
        self.assertEqual(ego4d_library.library_summary(self.index, self.root)['state'], 'ready')


if __name__ == '__main__':
    unittest.main()
