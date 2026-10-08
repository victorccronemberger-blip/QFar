"""curl/libcurl error codes stay transport failures, never HTTP statuses."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from moneymin import upload


class CurlTransportCodeTests(unittest.TestCase):
    class CurlError(Exception):
        def __init__(self, code):
            super().__init__(f"Failed to perform, curl: ({code}) network error")
            self.code = code

    def test_curl_codes_are_not_http_statuses(self):
        for code in (6, 28, 52, 56):
            with self.subTest(code=code):
                self.assertIsNone(upload._status_from_exception(self.CurlError(code)))

    def test_http_statuses_remain_distinguishable(self):
        class HttpError(Exception):
            status_code = 403

        class ResponseCodeError(Exception):
            code = 429

        self.assertEqual(upload._status_from_exception(HttpError()), 403)
        self.assertEqual(upload._status_from_exception(ResponseCodeError()), 429)

    def test_dns_error_becomes_retryable_transport_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "fixture.mp4"
            video.write_bytes(b"declared inert transport fixture")
            with patch.object(upload.transport, "put_blob_file",
                              side_effect=self.CurlError(6)):
                with self.assertRaises(upload.UploadError) as raised:
                    upload._put_blob_file("https://blob.invalid/fixture", video)

        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.phase, "transport")
        self.assertIsNone(raised.exception.status_code)


if __name__ == "__main__":
    unittest.main()
