"""Inert cleanup acknowledgements preserve status, identity and private diagnostics."""
import unittest

from moneymin import upload


class _Response:
    def __init__(self, status, body):
        self.status, self.body, self.calls = status, body, []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        return self.status, self.body


class UploadCleanupContractTests(unittest.TestCase):
    def test_fail_acknowledgement_requires_finite_object_and_matching_optional_ids(self):
        bad = ('', 'null', '[]', 'true', '5', '{"x":NaN}', '{"x":Infinity}',
               '{"x":1,"x":2}', '{"id":"other"}', '{"uploadId":null}',
               '{"upload_id":false}', '{"id":"known","uploadId":"other"}')
        for body in bad:
            with self.subTest(body=body):
                session = _Response(200, body)
                with self.assertRaises(upload.UploadError) as caught:
                    upload.fail_upload(session, 'known', 'Inert failure')
                error = caught.exception
                self.assertEqual(error.phase, 'fail')
                self.assertTrue(error.review_required)
                self.assertFalse(error.transient)
                self.assertEqual(len(session.calls), 1)

    def test_compatible_cleanup_acknowledgements_preserve_identity_and_truncate_message(self):
        for body in ('{}', '{"id":"known"}', '{"uploadId":"known","upload_id":"known","detail":null}'):
            with self.subTest(body=body):
                session = _Response(200, body)
                self.assertIsInstance(upload.fail_upload(session, 'known', 'x' * 501), dict)
                self.assertEqual(session.calls, [('PATCH', '/api/v1/uploads/known/fail',
                                                 {'error_message': 'x' * 500})])
        self.assertTrue(upload.delete_upload(_Response(204, ''), 'known'))
        self.assertTrue(upload.delete_session(_Response(204, ''), 'inert-org', 'inert-session'))

    def test_unsuccessful_cleanup_preserves_http_status_without_echoing_private_body(self):
        routes = (
            ('fail', lambda s: upload.fail_upload(s, 'known', 'Inert failure')),
            ('delete-upload', lambda s: upload.delete_upload(s, 'known')),
            ('delete-session', lambda s: upload.delete_session(s, 'inert-org', 'inert-session')),
        )
        private = 'https://blob.invalid/private?sig=PRIVATE_CLEANUP_FIXTURE'
        for phase, operation in routes:
            for status in (-1, 201, 400, 408, 429, 503):
                with self.subTest(phase=phase, status=status):
                    session = _Response(status, private)
                    with self.assertRaises(upload.UploadError) as caught:
                        operation(session)
                    error = caught.exception
                    self.assertEqual(error.phase, phase)
                    self.assertEqual(error.status_code, status)
                    self.assertEqual(error.transient, status in (-1, 408, 429, 503))
                    self.assertNotIn(private, str(error))
                    self.assertNotIn('PRIVATE_CLEANUP_FIXTURE', str(error))
                    self.assertEqual(len(session.calls), 1)
