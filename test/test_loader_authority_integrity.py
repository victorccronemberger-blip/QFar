"""Authority readers reject ambiguity before local effects; fixtures only."""
from contextlib import ExitStack
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from moneymin import atomic_io, config, recording_timeline, sent_registry, upload, upload_storage
from moneymin.web import server

OWNER = 'local-fixture@example.invalid'
OTHER = 'other-fixture@example.invalid'


class LoaderAuthorityIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='authority-fixture-')))
        self.data, self.media, self.secrets = (self.root / name for name in ('local', 'library', 'secrets'))
        for path in (self.data, self.media, self.secrets):
            path.mkdir()
        for name, value in (('DATA_DIR', self.data), ('MEDIA_DATA_DIR', self.media), ('SECRETS_DIR', self.secrets)):
            self.stack.enter_context(patch.object(config, name, value))
        self.stack.enter_context(patch.object(server, 'CROWTADO_PW_PATH', self.secrets / 'legacy_fixture.json'))
        self.stack.enter_context(patch.object(server.credential_store, 'load_all', return_value={}))
        self.stack.enter_context(patch.object(config, 'tokens_dir', return_value=self.secrets))
        self.stack.enter_context(patch.object(upload_storage.token_store, 'records', return_value={OWNER: {}}))

    def write(self, path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(payload, str):
            payload = payload.encode()
        path.write_bytes(payload)
        return payload

    def mirror_row(self):
        return json.dumps({OWNER: 'fixture-secret'})

    def source_row(self):
        return {'session_id': 'fixture-session', 'chunk_index': 0, 'account_email': OWNER,
                'upload_id': 'synthetic-receipt', 'state': 'done', 'recorded_at': '2026-10-03T10:00:00.000Z'}

    def assert_private(self, exception):
        text = str(exception)
        for canary in (OWNER, OTHER, 'fixture-secret', str(self.root), 'payload-canary'):
            self.assertNotIn(canary, text)
        self.assertTrue('preserv' in text.casefold() or 'restaur' in text.casefold())

    def test_sent_index_ambiguity_vetoes_selection_and_write_preserving_bytes(self):
        path = self.data / sent_registry.FILE_NAME
        cases = ['{"task":{"old":["local-fixture@example.invalid"]},"task":{}}',
                 '{"task":{"old":["local-fixture@example.invalid"],"old":[]}}',
                 '{"task":{"old":["payload-canary"]},"extra":NaN}',
                 '{"task":{"old":["payload-canary"]},"extra":1e999}',
                 '{broken payload-canary', 'null', '[1]', b'\xff']
        for text in cases:
            with self.subTest(text=text):
                before = self.write(path, text)
                with self.assertRaises(ValueError) as error:
                    sent_registry.is_sent_to_all('task', 'old', [OWNER])
                self.assert_private(error.exception)
                with self.assertRaises(ValueError):
                    sent_registry.mark_sent('task', 'new', OWNER)
                self.assertEqual(path.read_bytes(), before)

    def test_valid_sent_index_keeps_previous_delivery_and_adds_new(self):
        path = self.data / sent_registry.FILE_NAME
        self.write(path, '\ufeff' + json.dumps({'task': {'old': [OWNER]}}))
        self.assertTrue(sent_registry.is_sent_to_all('task', 'old', [OWNER]))
        sent_registry.mark_sent('task', 'new', OTHER)
        self.assertEqual(sent_registry.load(), {'task': {'old': [OWNER], 'new': [OTHER]}})

    def test_reset_barrier_ambiguity_vetoes_recovery_and_reseed(self):
        path = self.data / 'sent_reset_history.json'
        self.write(self.data / 'campaign_old.json', json.dumps({'items': [{'clip_uid': 'old', 'registry_key': 'task',
            'accounts': [{'email': OWNER, 'ok': True, 'finalized': True}]}]}))
        for text in ('{"completed_sessions":["fixture-session"],"completed_sessions":[]}',
                     '{"all":["campaign_old.json"],"all":[]}', '{"scenarios":{"task":[],"task":["campaign_old.json"]}}',
                     '{"all":[NaN]}', '{"all":[1]}', '{broken payload-canary'):
            with self.subTest(text=text):
                before = self.write(path, text)
                with self.assertRaises(ValueError) as error:
                    sent_registry.recovery_was_reset('fixture-session', 'task', 'campaign_old.json')
                self.assert_private(error.exception)
                with self.assertRaises(ValueError):
                    sent_registry.load()
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse((self.data / sent_registry.FILE_NAME).exists())

    def test_valid_reset_barrier_remains_effective(self):
        self.write(self.data / 'sent_reset_history.json', '{"completed_sessions":["fixture-session"],"all":["campaign_old.json"]}')
        self.assertTrue(sent_registry.recovery_was_reset('fixture-session', 'task'))
        self.assertTrue(sent_registry.recovery_reset_checker()('fixture-session', 'task', 'campaign_old.json'))

    def test_corrupt_timeline_vetoes_reservation_before_any_write(self):
        path = recording_timeline.timeline_path()
        cases = ['{broken payload-canary', 'null', '{"accounts":null}',
                 '{"accounts":{"local-fixture@example.invalid":{"last_end_epoch":9000,"last_end_epoch":0}}}',
                 '{"accounts":{"other-fixture@example.invalid":{"last_end_epoch":NaN}}}',
                 '{"accounts":{"other-fixture@example.invalid":{"last_end_epoch":true}}}',
                 '{"accounts":{"other-fixture@example.invalid":{"last_end_epoch":"payload-canary"}}}',
                 '{"accounts":{"other-fixture@example.invalid":[]}}']
        for text in cases:
            with self.subTest(text=text):
                before = self.write(path, text)
                with patch.object(recording_timeline, 'save_json') as save:
                    with self.assertRaises(ValueError) as error:
                        recording_timeline.reserve(OWNER, 60, now=1000)
                self.assert_private(error.exception)
                save.assert_not_called()
                self.assertEqual(path.read_bytes(), before)

    def test_valid_timeline_preserves_other_accounts_and_future_end(self):
        path = recording_timeline.timeline_path()
        other = {'last_end_epoch': '9500', 'historical_note': 'keep'}
        self.write(path, json.dumps({'version': 1, 'accounts': {OWNER: {'last_end_epoch': 9000}, OTHER: other}}))
        slot = recording_timeline.reserve(OWNER, 60, now=1000)
        self.assertEqual((slot.start_epoch, slot.end_epoch), (9000, 9060))
        self.assertEqual(json.loads(path.read_text())['accounts'][OTHER], other)

    def test_missing_timeline_and_indices_keep_legacy_creation_contract(self):
        slot = recording_timeline.reserve(OWNER, 60, now=1000)
        self.assertEqual((slot.start_epoch, slot.end_epoch), (1000, 1060))
        self.assertEqual(sent_registry.load(), {})
        self.assertEqual(sent_registry._reset_history(), {})

    def test_ambiguous_migration_source_cannot_be_canonicalized_or_acknowledged(self):
        legacy = self.media / 'sidecars'
        source = legacy / 'fixture-session.json'
        cases = ['{"session_id":"fixture-session","account_email":"other-fixture@example.invalid","account_email":"local-fixture@example.invalid"}',
                 '{"session_id":"fixture-session","account_email":"local-fixture@example.invalid","extra":{"x":1,"x":2}}',
                 '{"session_id":"fixture-session","account_email":"local-fixture@example.invalid","extra":1e999}',
                 '{broken payload-canary', 'null']
        for text in cases:
            with self.subTest(text=text):
                before = self.write(source, text)
                with self.assertRaises(ValueError) as error:
                    upload_storage.journal_directory()
                self.assert_private(error.exception)
                self.assertEqual(source.read_bytes(), before)
                self.assertFalse((self.data / 'sidecars' / source.name).exists())
                self.assertFalse((self.data / 'sidecar_migration.json').exists())

    def test_ambiguous_existing_migration_target_is_not_acknowledged(self):
        source = self.media / 'sidecars' / 'fixture-session.json'
        row = self.source_row()
        source_bytes = self.write(source, json.dumps(row))
        target = self.data / 'sidecars' / source.name
        before = self.write(target, json.dumps(row).replace('"upload_id": "synthetic-receipt"',
                           '"upload_id": "conflicting-receipt", "upload_id": "synthetic-receipt"'))
        with self.assertRaises(ValueError):
            upload_storage.journal_directory()
        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertEqual(target.read_bytes(), before)
        self.assertFalse((self.data / 'sidecar_migration.json').exists())

    def test_ambiguous_marker_vetoes_migration_preserving_previous_acknowledgments(self):
        legacy = self.media / 'sidecars'
        self.write(legacy / 'fixture-session.json', json.dumps(self.source_row()))
        key = json.dumps(str(legacy.resolve()))
        marker = self.data / 'sidecar_migration.json'
        before = self.write(marker, '{' + key + ':["already-imported.json"],' + key + ':[]}')
        with self.assertRaises(ValueError):
            upload_storage.journal_directory()
        self.assertEqual(marker.read_bytes(), before)
        self.assertFalse((self.data / 'sidecars' / 'fixture-session.json').exists())

    def test_valid_migration_preserves_owner_clock_receipt_archive_and_existing_target(self):
        legacy = self.media / 'sidecars'
        row = self.source_row()
        source = legacy / 'fixture-session.json'
        source_bytes = self.write(source, json.dumps(row))
        archive = source.with_suffix('.data.zip')
        archive_bytes = self.write(archive, b'synthetic archive bytes; not real capture')
        destination = upload_storage.journal_directory()
        actual = upload.load_sidecar('fixture-session')
        for key in ('session_id', 'account_email', 'upload_id', 'recorded_at'):
            self.assertEqual(actual[key], row[key])
        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertEqual((destination / archive.name).read_bytes(), archive_bytes)
        target_bytes = (destination / source.name).read_bytes()
        upload_storage.journal_directory()
        self.assertEqual((destination / source.name).read_bytes(), target_bytes)

    def test_valid_foreign_migration_source_still_ignored_without_ack(self):
        source = self.media / 'sidecars' / 'fixture-session.json'
        row = {**self.source_row(), 'account_email': OTHER}
        before = self.write(source, json.dumps(row))
        upload_storage.journal_directory()
        self.assertEqual(source.read_bytes(), before)
        self.assertFalse((self.data / 'sidecars' / source.name).exists())
        self.assertFalse((self.data / 'sidecar_migration.json').exists())

    def test_later_corrupt_source_or_target_vetoes_all_migration_publication(self):
        legacy = self.media / 'sidecars'
        first = legacy / 'first-session.json'
        later = legacy / 'later-session.json'
        first_bytes = self.write(first, json.dumps({**self.source_row(), 'session_id': 'first-session'}))
        archive = first.with_suffix('.data.zip')
        archive_bytes = self.write(archive, b'synthetic first archive')
        target_later = self.data / 'sidecars' / later.name
        for bad_location in ('source', 'target'):
            with self.subTest(bad_location=bad_location):
                target_later.unlink(missing_ok=True)
                later_bytes = self.write(later, '{broken payload-canary' if bad_location == 'source'
                                         else json.dumps({**self.source_row(), 'session_id': 'later-session'}))
                if bad_location == 'target':
                    target_bytes = self.write(target_later, '{broken payload-canary')
                with patch.object(Path, 'glob', return_value=iter((first, later))):
                    with self.assertRaises(ValueError):
                        upload_storage.journal_directory()
                self.assertEqual(first.read_bytes(), first_bytes)
                self.assertEqual(later.read_bytes(), later_bytes)
                self.assertEqual(archive.read_bytes(), archive_bytes)
                self.assertFalse((self.data / 'sidecars' / first.name).exists())
                self.assertFalse((self.data / 'sidecars' / archive.name).exists())
                self.assertFalse((self.data / 'sidecar_migration.json').exists())
                if bad_location == 'target':
                    self.assertEqual(target_later.read_bytes(), target_bytes)

    def test_later_conflicting_archive_vetoes_earlier_migration_publication(self):
        legacy = self.media / 'sidecars'
        first, later = (legacy / name for name in ('first-session.json', 'later-session.json'))
        self.write(first, json.dumps({**self.source_row(), 'session_id': 'first-session'}))
        self.write(later, json.dumps({**self.source_row(), 'session_id': 'later-session'}))
        archive = later.with_suffix('.data.zip')
        self.write(archive, b'synthetic source archive')
        destination_archive = self.data / 'sidecars' / archive.name
        before = self.write(destination_archive, b'different preserved synthetic archive')
        with patch.object(Path, 'glob', return_value=iter((first, later))):
            with self.assertRaises(ValueError):
                upload_storage.journal_directory()
        self.assertEqual(destination_archive.read_bytes(), before)
        self.assertFalse((self.data / 'sidecars' / first.name).exists())
        self.assertFalse((self.data / 'sidecars' / later.name).exists())
        self.assertFalse((self.data / 'sidecar_migration.json').exists())

    def test_io_failures_veto_consulted_authority_without_fallback_or_effect(self):
        mirror = server.CROWTADO_PW_PATH
        timeline = recording_timeline.timeline_path()
        sent = self.data / sent_registry.FILE_NAME
        source = self.media / 'sidecars' / 'fixture-session.json'
        payloads = [(mirror, self.mirror_row(), server._crowtado_creds),
                    (timeline, '{"accounts":{}}', lambda: recording_timeline.reserve(OWNER, 60, now=1000)),
                    (sent, '{"task":{}}', sent_registry.load),
                    (source, json.dumps(self.source_row()), upload_storage.journal_directory)]
        original = Path.read_text
        for path, payload, operation in payloads:
            with self.subTest(path=path):
                before = self.write(path, payload)
                def denied(current, *args, **kwargs):
                    if current == path:
                        raise PermissionError('payload-canary ' + str(self.root))
                    return original(current, *args, **kwargs)
                with patch.object(Path, 'read_text', denied):
                    with self.assertRaises(ValueError) as error:
                        operation()
                self.assert_private(error.exception)
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse((self.data / 'sidecar_migration.json').exists())

    def test_legacy_password_ambiguity_vetoes_promotion_and_primary_read(self):
        mirror = server.CROWTADO_PW_PATH
        batch = self.data / 'novas_contas_fixture.json'
        cases = [(mirror, '{"local-fixture@example.invalid":"fixture-secret","local-fixture@example.invalid":"fixture-other"}'),
                 (mirror, '{broken payload-canary'), (mirror, '[]'),
                 (batch, '[{"email":"local-fixture@example.invalid","password":"fixture-secret","password":"fixture-other"}]'),
                 (batch, '[{"email":"local-fixture@example.invalid","password":"fixture-secret","extra":NaN}]'),
                 (batch, '{}')]
        for path, text in cases:
            with self.subTest(path=path, text=text):
                mirror.unlink(missing_ok=True)
                batch.unlink(missing_ok=True)
                before = self.write(path, text)
                with patch.object(server.credential_store, 'load_all') as primary, \
                     patch.object(server.credential_store, 'save') as save:
                    with self.assertRaises(ValueError) as error:
                        server._configured_crowtado_creds()
                self.assert_private(error.exception)
                primary.assert_not_called()
                save.assert_not_called()
                self.assertEqual(path.read_bytes(), before)

    def test_primary_precedence_and_valid_legacy_records_remain_unchanged(self):
        mirror = server.CROWTADO_PW_PATH
        batch = self.data / 'novas_contas_fixture.json'
        batch_bytes = self.write(batch, json.dumps([{'email': OWNER.upper(), 'senha': ' old fixture '}, None]))
        mirror_bytes = self.write(mirror, json.dumps({OWNER.upper(): 'mirror fixture'}))
        with patch.object(server.credential_store, 'load_all', return_value={OWNER: 'primary fixture'}):
            self.assertEqual(server._crowtado_creds(), {OWNER: 'primary fixture'})
        self.assertEqual(server._crowtado_creds(), {OWNER: 'mirror fixture'})
        self.assertEqual(mirror.read_bytes(), mirror_bytes)
        self.assertEqual(batch.read_bytes(), batch_bytes)

    def test_missing_legacy_files_allow_primary_credential_and_valid_promotion(self):
        with patch.object(server.credential_store, 'load_all', return_value={OWNER: 'primary fixture'}):
            self.assertEqual(server._crowtado_creds(), {OWNER: 'primary fixture'})
        self.write(server.CROWTADO_PW_PATH, self.mirror_row())
        with patch.object(server, '_list_accounts', return_value=[{'email': OWNER}]), \
             patch.object(server.org_policy, 'account_kind', return_value='crowtado'), \
             patch.object(server.credential_store, 'lookup', return_value=None), \
             patch.object(server.credential_store, 'save') as save:
            self.assertEqual(server._configured_crowtado_creds(), {OWNER: 'fixture-secret'})
        save.assert_called_once_with(self.secrets, OWNER, 'fixture-secret')

    def test_corrupt_legacy_mirror_vetoes_removal_before_primary_delete_or_cache_clear(self):
        mirror = server.CROWTADO_PW_PATH
        for text in ('{"local-fixture@example.invalid":"fixture-secret","local-fixture@example.invalid":"fixture-other","other-fixture@example.invalid":"delete fixture"}',
                     '{broken payload-canary', 'null'):
            with self.subTest(text=text):
                before = self.write(mirror, text)
                with patch.object(server.credential_store, 'delete') as delete, \
                     patch.object(server, '_load_balances') as balances, \
                     patch.object(server.crowtado, 'clear_cached_session') as cache:
                    with self.assertRaises(ValueError) as error:
                        server._remove_account_data(OTHER)
                self.assert_private(error.exception)
                delete.assert_not_called()
                balances.assert_not_called()
                cache.assert_not_called()
                self.assertEqual(mirror.read_bytes(), before)

    def test_valid_other_account_removal_preserves_survivor_and_primary_delete_order(self):
        mirror = server.CROWTADO_PW_PATH
        self.write(mirror, json.dumps({OWNER: 'fixture-secret', OTHER.upper(): 'delete fixture'}))
        with patch.object(server.credential_store, 'delete') as delete, \
             patch.object(server, '_load_balances', return_value={}), \
             patch.object(server.crowtado, 'clear_cached_session') as cache:
            server._remove_account_data(OTHER)
        delete.assert_called_once_with(self.secrets, OTHER)
        cache.assert_called_once_with(OTHER)
        self.assertEqual(atomic_io.load_json_state(mirror, {}), {OWNER: 'fixture-secret'})

    def password_client(self):
        self.stack.enter_context(patch.object(server, '_list_accounts', return_value=[{'email': OWNER}]))
        self.stack.enter_context(patch.object(server.banned_store, 'load', return_value={'accounts': [{'email': OWNER}]}))
        return server.create_app(for_testing=True).test_client()

    def test_password_routes_reveal_valid_primary_without_consulting_corrupt_legacy(self):
        client = self.password_client()
        before = self.write(server.CROWTADO_PW_PATH, '{broken payload-canary')
        password = ' fixture whitespace password '
        with patch.object(server.credential_store, 'lookup', return_value=password), \
             patch.object(server, '_crowtado_creds', wraps=server._crowtado_creds) as legacy, \
             patch.object(server.credential_store, 'save') as save:
            for route in ('/api/accounts/password', '/api/accounts/banned/password'):
                with self.subTest(route=route):
                    response = client.post(route, json={'email': OWNER.upper()})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.get_json(), {'email': OWNER, 'password': password})
                    self.assertEqual(response.headers['Cache-Control'], 'no-store')
        legacy.assert_not_called()
        save.assert_not_called()
        self.assertEqual(server.CROWTADO_PW_PATH.read_bytes(), before)

    def test_password_routes_missing_primary_with_corrupt_consulted_legacy_fail_privately(self):
        client = self.password_client()
        before = self.write(server.CROWTADO_PW_PATH, '{broken payload-canary')
        with patch.object(server.credential_store, 'lookup', return_value=None), \
             patch.object(server.credential_store, 'save') as save, \
             patch.object(server.credential_store, 'delete') as delete:
            for route in ('/api/accounts/password', '/api/accounts/banned/password'):
                with self.subTest(route=route):
                    response = client.post(route, json={'email': OWNER})
                    self.assertEqual(response.status_code, 409)
                    self.assertNotIn('password', response.get_json())
                    self.assertNotIn('payload-canary', response.get_data(as_text=True))
                    self.assertNotIn(str(self.root), response.get_data(as_text=True))
        save.assert_not_called()
        delete.assert_not_called()
        self.assertEqual(server.CROWTADO_PW_PATH.read_bytes(), before)

    def test_password_routes_corrupt_primary_vetoes_valid_legacy_without_consulting_it(self):
        client = self.password_client()
        before = self.write(server.CROWTADO_PW_PATH, self.mirror_row())
        with patch.object(server.credential_store, 'lookup', side_effect=ValueError('private primary fixture')), \
             patch.object(server, '_crowtado_creds', wraps=server._crowtado_creds) as legacy:
            for route in ('/api/accounts/password', '/api/accounts/banned/password'):
                with self.subTest(route=route):
                    response = client.post(route, json={'email': OWNER})
                    self.assertEqual(response.status_code, 404)
                    self.assertNotIn('fixture-secret', response.get_data(as_text=True))
                    self.assertNotIn('password', response.get_json())
        legacy.assert_not_called()
        self.assertEqual(server.CROWTADO_PW_PATH.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
