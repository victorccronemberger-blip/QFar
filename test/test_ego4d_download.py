from __future__ import annotations

import tempfile
import hashlib
import threading
import unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from moneymin import ego4d


class Ego4dDownloadTests(unittest.TestCase):
    def test_parallel_download_publishes_only_complete_file(self):
        resource = Mock()
        original = b"existing complete video"
        payload = b"new complete video"
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "video.mp4"
            dest.write_bytes(original)

            def download(key, target, *, Config):
                self.assertEqual(dest.read_bytes(), original)
                self.assertGreater(Config.max_concurrency, 10)
                self.assertIsNone(Config.max_bandwidth)
                self.assertLessEqual(Config.max_io_queue * Config.io_chunksize, 64 * 1024**2)
                Path(target).write_bytes(payload)

            resource.Bucket.return_value.download_file.side_effect = download
            with patch.object(ego4d, "_s3", return_value=resource):
                self.assertEqual(ego4d._download_to("fixture", "clip.mp4", dest), dest)
            self.assertEqual(dest.read_bytes(), payload)
            self.assertEqual(list(Path(folder).iterdir()), [dest])

    def test_interrupted_download_preserves_existing_file_and_cleans_partial(self):
        resource = Mock()
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "video.mp4"
            dest.write_bytes(b"original")

            def download(key, target, *, Config):
                Path(target).write_bytes(b"partial")
                raise OSError("connection interrupted")

            resource.Bucket.return_value.download_file.side_effect = download
            with patch.object(ego4d, "_s3", return_value=resource), \
                    self.assertRaisesRegex(OSError, "interrupted"):
                ego4d._download_to("fixture", "clip.mp4", dest)
            self.assertEqual(dest.read_bytes(), b"original")
            self.assertEqual(list(Path(folder).iterdir()), [dest])

    def test_connection_pool_can_reuse_all_parallel_requests(self):
        with patch("boto3.session.Session") as session:
            ego4d._s3()
        settings = session.return_value.resource.call_args.kwargs["config"]
        self.assertGreaterEqual(settings.max_pool_connections,
                                ego4d._download_config().max_concurrency)
        self.assertTrue(settings.tcp_keepalive)
        self.assertNotEqual(settings.retries["mode"], "adaptive")

    def test_sdk_queue_recovers_failed_part_without_repeating_completed_parts(self):
        import boto3
        from botocore.config import Config

        payload = bytes(range(256)) * (12 * 1024**2 // 256)
        etag = '"' + hashlib.md5(payload).hexdigest() + '"'
        requests = Counter()
        guard = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_HEAD(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("ETag", etag)
                self.end_headers()

            def do_GET(self):
                part = self.headers.get("Range", "")
                with guard:
                    requests[part] += 1
                    first_attempt = requests[part] == 1
                if part.startswith("bytes=0-") and first_attempt:
                    error = b"<Error><Code>SlowDown</Code></Error>"
                    self.send_response(503)
                    self.send_header("Content-Type", "application/xml")
                    self.send_header("Content-Length", str(len(error)))
                    self.end_headers()
                    self.wfile.write(error)
                    return
                start, end = part.removeprefix("bytes=").split("-", 1)
                start = int(start)
                end = int(end) if end else len(payload) - 1
                body = payload[start:end+1]
                self.send_response(206)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
                self.send_header("ETag", etag)
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        resource = boto3.session.Session().resource(
            "s3", endpoint_url=f"http://127.0.0.1:{server.server_port}",
            region_name="us-east-1", aws_access_key_id="local-test",
            aws_secret_access_key="local-test",
            config=Config(retries={"mode": "standard", "total_max_attempts": 3}))
        try:
            with tempfile.TemporaryDirectory() as folder:
                dest = Path(folder) / "download.mp4"
                with patch.object(ego4d, "_s3", return_value=resource):
                    ego4d._download_to("fixture", "video", dest)
                self.assertEqual(dest.read_bytes(), payload)
                self.assertEqual(list(Path(folder).iterdir()), [dest])
            self.assertEqual(len(requests), 6)
            self.assertEqual(requests["bytes=0-2097151"], 2)
            self.assertTrue(all(count == 1 for key, count in requests.items()
                                if key != "bytes=0-2097151"))
        finally:
            resource.meta.client.close()
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
