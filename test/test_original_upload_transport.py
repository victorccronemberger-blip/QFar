"""Original bytes/clock/receipt guards with inert MP4, HTTP and probe.

ZIP/CSV inspection and validation and the transport stream are real. These
fixtures establish software behavior, never camera, IMU or provider acceptance.
"""
from contextlib import ExitStack
from dataclasses import replace
import hashlib
import inspect
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from moneymin import config, transport, upload
from moneymin.capture_import import inspect_original_capture
from moneymin.minute_api import AuthError
from test_original_capture_workflow import fixture_pair, fixture_probe, EPOCH, OWNER, ORG, TASK, SID


class BlobClient:
    """No socket; retained staged bodies and explicit BlockList calls."""
    def __init__(self, mutation=None):
        self.calls = []; self.closed = 0; self.mutation = mutation
    def put(self, url, **kwargs):
        self.calls.append((url, bytes(kwargs['data'])))
        if self.mutation and parse_qs(urlsplit(url).query).get('comp') == ['block']:
            action, self.mutation = self.mutation, None
            action()
        return SimpleNamespace(status_code=201)
    def close(self):
        self.closed += 1


class OriginalTransportHashTests(unittest.TestCase):
    def setUp(self):
        self.assertIn('expected_sha256', inspect.signature(transport.put_blob_file).parameters,
                      'R15 feature gap: reviewed source hash is not checked while streaming')
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.path = root/'private-path.mp4'
        self.data = bytes(range(250)) * 100
        self.path.write_bytes(self.data)
        self.sha = hashlib.sha256(self.data).hexdigest()
        self.stack.enter_context(patch.object(transport, 'BLOCK_SIZE', 4096))
    def configure(self, client=None):
        self.stack.enter_context(patch.object(transport, '_kind', 'curl' if client else 'urllib'))
        self.stack.enter_context(patch.object(transport, '_cffi', SimpleNamespace(Session=lambda **_: client) if client else None))
    def mutate(self):
        with self.path.open('r+b') as stream:
            stream.seek(-1, 2); stream.write(b'!')
    def test_native_commit_contains_exact_reviewed_bytes(self):
        client = BlobClient(); self.configure(client)
        self.assertEqual(transport.put_blob_file('https://blob.invalid/media?sig=fixture', self.path,
                         expected_sha256=self.sha), 201)
        blocks = [body for url, body in client.calls if parse_qs(urlsplit(url).query).get('comp') == ['block']]
        self.assertEqual(b''.join(blocks), self.data)
        self.assertEqual(sum(parse_qs(urlsplit(url).query).get('comp') == ['blocklist'] for url, _ in client.calls), 1)
        self.assertEqual(client.closed, 1)
    def test_native_mutation_never_commits_blocklist(self):
        client = BlobClient(self.mutate); self.configure(client)
        with self.assertRaises(ValueError) as raised:
            transport.put_blob_file('https://blob.invalid/media?sig=private', self.path, expected_sha256=self.sha)
        self.assertGreater(len(client.calls), 0)
        self.assertFalse(any(parse_qs(urlsplit(url).query).get('comp') == ['blocklist'] for url, _ in client.calls))
        self.assertEqual(client.closed, 1)
        self.assertNotIn(str(self.path), str(raised.exception)); self.assertNotIn('sig=', str(raised.exception))
    def test_urllib_full_consumer_retains_exact_body(self):
        self.configure(); bodies = []
        def consumer(req, **_):
            while data := req.data.read(4096): bodies.append(data)
            response = Mock(); response.__enter__ = Mock(return_value=SimpleNamespace(status=201))
            response.__exit__ = Mock(return_value=False)
            return response
        with patch.object(transport.tls, 'urlopen', side_effect=consumer):
            self.assertEqual(transport.put_blob_file('https://blob.invalid/media', self.path, expected_sha256=self.sha), 201)
        self.assertEqual(b''.join(bodies), self.data)
    def test_urllib_false_ack_and_changed_body_are_rejected(self):
        self.configure()
        for mode in ('partial', 'changed'):
            with self.subTest(mode=mode):
                self.path.write_bytes(self.data); emitted = []
                def consumer(req, **_):
                    emitted.append(req.data.read(4096))
                    if mode == 'changed':
                        self.mutate()
                        while data := req.data.read(4096): emitted.append(data)
                    response = Mock(); response.__enter__ = Mock(return_value=SimpleNamespace(status=201))
                    response.__exit__ = Mock(return_value=False)
                    return response
                with patch.object(transport.tls, 'urlopen', side_effect=consumer):
                    with self.assertRaises(ValueError):
                        transport.put_blob_file('https://blob.invalid/media', self.path, expected_sha256=self.sha)
                self.assertLess(sum(map(len, emitted)), len(self.data))
    def test_malformed_hash_is_rejected_before_backend_or_file_access(self):
        for value in ('', 'A'*64, '0'*63, '0'*65, False, 7):
            with self.subTest(value=value), patch.object(transport, '_resolve') as resolver:
                with self.assertRaises(ValueError):
                    transport.put_blob_file('https://blob.invalid/media', self.path, expected_sha256=value)
                resolver.assert_not_called()


class LegacyTransportControlTests(unittest.TestCase):
    def test_optional_hash_keeps_existing_stream_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'declared-legacy.mp4'; data = b'Declared inert legacy stream control'
            path.write_bytes(data); client = BlobClient()
            with (patch.object(transport, '_kind', 'curl'), patch.object(transport, 'BLOCK_SIZE', 8),
                  patch.object(transport, '_cffi', SimpleNamespace(Session=lambda **_: client))):
                self.assertEqual(transport.put_blob_file('https://blob.invalid/control', path), 201)
            blocks = [body for url, body in client.calls if parse_qs(urlsplit(url).query).get('comp') == ['block']]
            self.assertEqual(b''.join(blocks), data); self.assertEqual(client.closed, 1)


class CaptureSession:
    email = OWNER
    def __init__(self): self.calls = []
    def _check_write_policy(self, method, path, body):
        # Explicitly inert local policy observation, never provider authority.
        if method != 'POST' or not path.startswith('/api/v1/uploads?') or not body['meta'].get('cameras'):
            raise AssertionError('Invalid declared fixture policy body')
    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.startswith('/api/v1/uploads?'):
            return 201, json.dumps({'id': f'original_receipt_{body["meta"]["chunk_index"]}', 'status': 'initiated', 'meta': {}})
        if path == '/api/v1/storage/sas/blobs':
            return 200, json.dumps({'signed_urls': [{'filename': f['filename'], 'blob_url': 'https://blob.invalid/'+f['filename'],
                                   'expires_at': 'declared fixture string'} for f in body['files']]})
        if path.endswith('/complete'):
            return 200, json.dumps({'id': path.split('/')[-2], 'status': 'uploaded', 'meta': {}})
        if path.endswith('/evaluate'):
            return 200, json.dumps({'upload_id': path.split('/')[-2], 'checks': [
                {'id': 'declared_fixture_check', 'label': 'Declared inert response', 'status': 'pass', 'detail': None}]})
        if path.endswith('/finalize'): return 204, ''
        raise AssertionError('Unexpected inert capture operation')


class OriginalUploadGroupTests(unittest.TestCase):
    def setUp(self):
        self.assertIn('original_captures', inspect.signature(upload.upload_session).parameters,
                      'R15 feature gap: no byte-preserving original capture upload path')
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.journals = self.root/'state'/'journals'
        self.stack.enter_context(patch.multiple(config, DATA_DIR=self.root/'state', MEDIA_DATA_DIR=self.root/'library'))
        self.stack.enter_context(patch.object(upload, 'sidecars_dir', return_value=self.journals))
        self.stack.enter_context(patch.object(upload, 'probe_video', side_effect=fixture_probe))
        self.stack.enter_context(patch.object(upload.time, 'time', return_value=EPOCH))
        self.client = BlobClient()
        self.stack.enter_context(patch.object(transport, '_kind', 'curl'))
        self.stack.enter_context(patch.object(transport, '_cffi', SimpleNamespace(Session=lambda **_: self.client, put=self.client.put)))
        self.stack.enter_context(patch.object(transport, 'BLOCK_SIZE', 4096))
        self.session = CaptureSession()
        self.pairs = [fixture_pair(self.root/'source', i, count=2) for i in (0, 1)]
        self.captures = [inspect_original_capture(*pair) for pair in self.pairs]
    def send(self, **changes):
        args = dict(session=self.session, video_path=[c.media.path for c in self.captures], org_key=ORG, task_id=TASK,
                    session_id=SID, recorded_at=[c.metadata['createdAt'] for c in self.captures],
                    original_captures=self.captures, normalize=False, profile=None, sidecar=True,
                    register_first=True, persist_sidecar=True, finalize=True, evaluate=True, max_retries=1)
        args.update(changes)
        return upload.upload_session(**args)
    def assert_pre_effect_rejection(self, operation):
        with self.assertRaises(upload.UploadError) as raised: operation()
        self.assertEqual(self.session.calls, []); self.assertEqual(self.client.calls, [])
        self.assertFalse(self.journals.exists())
        self.assertNotIn(str(self.root), str(raised.exception))
    def test_whole_group_preserves_source_short_post_zip_identity_and_clock(self):
        before = [p.read_bytes() for pair in self.pairs for p in pair]
        with (patch.object(upload, 'normalize_video', side_effect=AssertionError('original reencode')),
              patch.object(upload, 'build_sidecar_zip', side_effect=AssertionError('original sidecar rebuild')),
              patch.object(upload, 'default_device_meta', side_effect=AssertionError('invented device'))):
            result = self.send()
        self.assertTrue(result.finalized, [(c.state, c.error) for c in result.chunks]); self.assertEqual(len(result.chunks), 2)
        creates = [body for _, path, body in self.session.calls if path.startswith('/api/v1/uploads?')]
        for i, body in enumerate(creates):
            meta = self.captures[i].metadata
            self.assertEqual(body['session_id'], SID); self.assertEqual(body['log_id'], f'{SID}_{i}')
            self.assertEqual(body['recorded_at'], meta['createdAt']); self.assertEqual(body['duration_ms'], meta['durationMs'])
            self.assertEqual(body['meta']['appVersion'], meta['appVersion'])
            self.assertEqual(body['meta']['device'], {'model': meta['device']['model']})
            self.assertEqual(body['meta']['platform'], {'os': 'android'})
            row = upload.load_sidecar(SID, i)
            self.assertEqual(row['original_media_sha256'], self.captures[i].media.sha256)
            self.assertEqual(row['original_sidecar_sha256'], self.captures[i].sidecar.sha256)
            self.assertIs(row['physical_provenance_verified'], False)
            for kind, original_path in (('mp4', self.pairs[i][0]), ('data.zip', self.pairs[i][1])):
                blocks = [data for url, data in self.client.calls if urlsplit(url).path.endswith(f'{SID}_{i}.{kind}')
                          and parse_qs(urlsplit(url).query).get('comp') == ['block']]
                self.assertEqual(b''.join(blocks), original_path.read_bytes())
        self.assertEqual(before, [p.read_bytes() for pair in self.pairs for p in pair])
        self.assertTrue(all('session_complete' not in body for _, path, body in self.session.calls if path.endswith('/complete')))
        self.assertEqual(sum(path.endswith('/finalize') for _, path, _ in self.session.calls), 1)
    def test_invalid_last_part_prevents_first_create_and_journal(self):
        self.pairs[1][0].write_bytes(b'changed last media')
        self.assert_pre_effect_rejection(self.send)
    def test_incomplete_reordered_or_retargeted_group_cannot_send(self):
        for change in ({'original_captures': self.captures[:1]}, {'original_captures': list(reversed(self.captures))},
                       {'session_id': SID+'_replacement'}, {'recorded_at': [c.metadata['createdAt'] for c in reversed(self.captures)]},
                       {'normalize': True}, {'profile': SimpleNamespace(email=OWNER)}):
            with self.subTest(change=list(change)): self.assert_pre_effect_rejection(lambda: self.send(**change))
    def test_owner_task_and_archive_override_are_rejected(self):
        self.session.email = ''
        self.assert_pre_effect_rejection(self.send)
        self.session.email = OWNER
        self.assert_pre_effect_rejection(lambda: self.send(sidecar_data=[b'changed', b'changed']))
        media, archive = fixture_pair(self.root/'other', 1, count=2,
                                    change=lambda meta: meta.update(taskId='other_task'))
        self.captures[1] = inspect_original_capture(media, archive)
        self.assert_pre_effect_rejection(self.send)
    def test_missing_or_conflicting_source_fields_cannot_fall_back_to_defaults(self):
        mutations = [lambda m: m['device'].pop('model'), lambda m: m.update(appVersion=''),
                     lambda m: m.pop('cameras'), lambda m: m.update(cameras=[]),
                     lambda m: m.update(cameras=[{'source': 'undeclared'}]),
                     lambda m: m.update(ownerEmail=None), lambda m: m['session'].update(taskId='other_task'),
                     lambda m: m['session'].update(chunkCount=True), lambda m: m['session'].update(chunkCount=None),
                     lambda m: m.update(createdAt='2027-01-15T07:56:00.000000Z'),
                     lambda m: m.update(durationMs=61_000),
                     lambda m: m['device'].update(model='OTHER DECLARED FIXTURE DEVICE')]
        for index, mutation in enumerate(mutations):
            with self.subTest(case=index):
                pair = fixture_pair(self.root/'negative', 1, count=2, change=mutation)
                self.captures[1] = inspect_original_capture(*pair)
                self.assert_pre_effect_rejection(self.send)
    def test_native_platform_type_and_explicit_source_bindings_are_preserved(self):
        def native(meta):
            meta['platform'] = {'type': 'android', 'version': 33}
            meta.update(accountEmail=OWNER, orgKey=ORG, taskId=TASK)
        self.pairs = [fixture_pair(self.root/'native', i, count=2, change=native) for i in (0, 1)]
        self.captures = [inspect_original_capture(*pair) for pair in self.pairs]
        result = self.send()
        self.assertTrue(result.finalized)
        bodies = [body for _, path, body in self.session.calls if path.startswith('/api/v1/uploads?')]
        self.assertTrue(all(body['meta']['platform'] == {'os': 'android'} for body in bodies))
        self.assertTrue(all(c.metadata['platform'] == {'type': 'android', 'version': 33} for c in self.captures))
    def test_mutation_during_create_pause_has_no_remote_create(self):
        def progress(phase, *_args, **_kw):
            if phase == 'create': self.pairs[0][0].write_bytes(b'changed after pause')
        result = self.send(on_progress=progress)
        self.assertFalse(result.finalized)
        self.assertFalse(any(path.startswith('/api/v1/uploads?') and body['meta']['chunk_index'] == 0
                             for _, path, body in self.session.calls))
        self.assertEqual(upload.load_sidecar(SID, 0)['state'], upload.STATE_QUARANTINE)
    def test_mid_transport_change_preserves_receipt_without_complete_or_fail(self):
        self.pairs[0][0].write_bytes(bytes(range(250))*100)
        self.captures[0] = inspect_original_capture(*self.pairs[0])
        def change():
            with self.pairs[0][0].open('r+b') as stream: stream.seek(-1, 2); stream.write(b'!')
        self.client.mutation = change
        result = self.send()
        self.assertFalse(result.finalized)
        first = upload.load_sidecar(SID, 0)
        self.assertEqual(first['upload_id'], 'original_receipt_0')
        self.assertEqual(first['phase'], 'transport_review'); self.assertEqual(first['state'], upload.STATE_QUARANTINE)
        self.assertFalse(any('original_receipt_0/complete' in path or path.endswith('/fail') or method == 'DELETE'
                             for method, path, _ in self.session.calls))
        self.assertFalse(any(urlsplit(url).path.endswith(f'{SID}_0.mp4')
                             and parse_qs(urlsplit(url).query).get('comp') == ['blocklist'] for url, _ in self.client.calls))
    def test_known_receipt_cannot_create_again(self):
        self.send(); before = list(self.session.calls)
        with self.assertRaises(upload.UploadError): self.send()
        self.assertEqual(self.session.calls, before)
    def test_last_part_current_policy_rejection_precedes_first_journal_and_create(self):
        self.session._check_write_policy = Mock(side_effect=[None, AuthError('Declared policy denial', code='policy')])
        with self.assertRaises(AuthError): self.send()
        self.assertEqual(self.session._check_write_policy.call_count, 2)
        self.assertEqual(self.session.calls, []); self.assertEqual(self.client.calls, [])
        self.assertFalse(self.journals.exists())
    def test_direct_original_never_upgrades_unowned_queued_journal(self):
        row = {'session_id': SID, 'chunk_index': 0, 'state': 'creating', 'phase': 'queued',
               'create_attempted': False, 'org_key': ORG, 'task_id': TASK,
               'recorded_at': self.captures[0].metadata['createdAt']}
        upload.save_sidecar(row)
        path = upload._sidecar_path(SID, 0); before = path.read_bytes()
        with self.assertRaises(upload.UploadError): self.send()
        self.assertEqual(self.session.calls, []); self.assertEqual(self.client.calls, [])
        self.assertEqual(path.read_bytes(), before)
