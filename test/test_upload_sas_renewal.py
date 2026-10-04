"""Failed SAS renewal must not retry a known stale artifact authorization."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from moneymin import upload


class UploadSasRenewalTests(unittest.TestCase):
    def execute(self, renewal_status, *, recovers=False):
        calls, puts = [], []
        sas_count = 0

        class FixtureSession:
            def request(self, method, path, body=None):
                nonlocal sas_count
                calls.append((method, path, body))
                if path.startswith('/api/v1/uploads?'):
                    return 201, json.dumps({'id': 'existing-fixture-upload', 'status': 'initiated', 'meta': {}})
                if path == '/api/v1/storage/sas/blobs':
                    sas_count += 1
                    if sas_count > 1 and not (recovers and sas_count > 2):
                        return renewal_status, '{}'
                    signature = 'renewed' if sas_count > 1 else 'expired'
                    return 200, json.dumps({'signed_urls': [
                        {'filename': row['filename'], 'blob_url':
                         f"https://blob.invalid/{row['filename']}?fixture={signature}", 'expires_at': '2030-01-01T00:00:00Z'}
                        for row in body['files']]})
                if path.endswith('/complete'):
                    return 200, json.dumps({'id': path.split('/')[-2], 'status': 'uploaded', 'meta': {}})
                if path.endswith('/fail'):
                    return 200, '{}'
                raise AssertionError('Unexpected fixture operation')

        def put_video(url, *args, **kwargs):
            puts.append(('video', url))
            return 201

        def put_zip(url, *args, **kwargs):
            puts.append(('zip', url))
            if 'expired' in url:
                raise upload.UploadError('fixture expired SAS', status_code=403, transient=False)
            return 201

        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            path = Path(temporary) / 'opaque-fixture.mp4'
            path.write_bytes(b'Protocol fixture only, not a recording.')
            for context in (patch.object(upload, '_probe_duration_ms', return_value=60_000),
                            patch.object(upload, '_validate_sidecar_zip', return_value={'fixture': True}),
                            patch('moneymin.validate.validate_upload_meta', return_value=[]),
                            patch.object(upload, '_put_blob_file', side_effect=put_video),
                            patch.object(upload, '_put_blob', side_effect=put_zip),
                            patch.object(upload.time, 'sleep', return_value=None)):
                stack.enter_context(context)
            result = upload._upload_single_chunk(FixtureSession(), path, 'fixture-org',
                'sas-fixture-session', 0, 'fixture-task', 'video/mp4', 10,
                '2020-01-01T00:00:00.000Z', None, None, None, None,
                sidecar=True, sidecar_data=b'Opaque ZIP fixture, not sensor data.', max_retries=2)
        return result, calls, puts

    def assert_transient_preserved(self, status):
        result, calls, puts = self.execute(status)
        self.assertEqual(result.state, upload.STATE_RETRY_LATE)
        self.assertIn(str(status), result.error)
        self.assertEqual(result.upload_id, 'existing-fixture-upload')
        self.assertEqual(len(puts), 2, 'failed renewal cannot send either artifact with the stale SAS again')
        self.assertEqual(sum(path.startswith('/api/v1/uploads?') for _, path, _ in calls), 1)
        self.assertEqual(sum(path == '/api/v1/storage/sas/blobs' for _, path, _ in calls), 3)
        self.assertFalse(any(path.endswith('/fail') or path.endswith('/complete') for _, path, _ in calls))

    def test_exhausted_503_renewal_preserves_transient_cause_and_receipt(self):
        self.assert_transient_preserved(503)

    def test_exhausted_429_renewal_preserves_rate_limit_cause_and_receipt(self):
        self.assert_transient_preserved(429)

    def test_recovered_renewal_keeps_single_create_and_completes_with_fresh_sas(self):
        result, calls, puts = self.execute(503, recovers=True)
        self.assertEqual(result.state, upload.STATE_DONE)
        self.assertEqual(result.upload_id, 'existing-fixture-upload')
        self.assertEqual([kind for kind, _ in puts], ['video', 'zip', 'video', 'zip'])
        self.assertTrue(all('renewed' in url for _, url in puts[2:]))
        self.assertEqual(sum(path.endswith('/complete') for _, path, _ in calls), 1)
        self.assertFalse(any(path.endswith('/fail') for _, path, _ in calls))

    def test_permanent_renewal_failure_preserves_its_cause_and_compensates_once(self):
        result, calls, puts = self.execute(422)
        self.assertEqual(result.state, upload.STATE_FAILED)
        self.assertIn('422', result.error)
        self.assertEqual(len(puts), 2)
        self.assertEqual(sum(path.endswith('/fail') for _, path, _ in calls), 1)
        self.assertFalse(any(path.endswith('/complete') for _, path, _ in calls))
