"""Sanitizing a CREATE adapter failure preserves its typed diagnostic cause."""
import unittest
from unittest.mock import patch

from moneymin import minute_api, upload
import test_upload_create_retry_uncertainty as retry_fixtures


class UploadCreateAdapterContractTests(unittest.TestCase):
    def setUp(self):
        fixture = retry_fixtures.UploadCreateRetryUncertaintyTests(
            'test_unknown_http_acknowledgements_do_not_create_replacement_receipts')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture

    def session(self, error):
        session = self.fixture.session('timeout')
        real_request = session.request
        def request(method, path, body=None):
            if path.startswith('/api/v1/uploads?'):
                session.calls.append((method, path, body))
                raise error
            return real_request(method, path, body)
        session.request = request
        return session

    def test_typed_upload_errors_keep_status_retry_class_and_block_reason(self):
        for status, transient, blocked in [(403, False, 'device'), (503, True, 'service')]:
            with self.subTest(status=status):
                original = upload.UploadError('private-adapter-canary', status_code=status,
                    transient=transient, blocked_reason=blocked, phase='external-adapter')
                session = self.session(original)
                observed = []
                retry = upload._with_retry
                def capture(fn, **kwargs):
                    try:
                        return retry(fn, **kwargs)
                    except upload.UploadError as error:
                        observed.append(error)
                        raise
                with patch.object(upload, '_with_retry', side_effect=capture):
                    result = self.fixture.send(session, 'typed-' + str(status))
                self.assertEqual(len(observed), 1)
                error = observed[0]
                self.assertEqual(error.status_code, status)
                self.assertIs(error.transient, transient)
                self.assertEqual(error.blocked_reason, blocked)
                self.assertEqual(error.phase, 'create')
                self.assertEqual(error.attempts, 1)
                self.assertNotIn('private-adapter-canary', str(error))
                self.assertIn(str(status), result.chunks[0].error)
                self.assertEqual(result.chunks[0].state,
                    upload.STATE_RETRY_LATE if transient else upload.STATE_FAILED)
                self.assertEqual(sum(path.startswith('/api/v1/uploads?') for _, path, _ in session.calls), 1)
                self.fixture.fixture.video_put.assert_not_called()

    def test_typed_auth_error_retains_its_account_issue_code(self):
        original = minute_api.AuthError('Identidade local incompatível.', code='identity')
        session = self.session(original)
        with self.assertRaises(minute_api.AuthError) as caught:
            self.fixture.send(session, 'auth-identity')
        self.assertIs(caught.exception, original)
        self.assertEqual(caught.exception.account_issue_code, 'identity')
        self.fixture.fixture.video_put.assert_not_called()
