"""Offline acquisition receipts and space checks, using inert source bytes."""
from collections import namedtuple
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from moneymin import ego4d


Disk = namedtuple("Disk", "total used free")


class ManagedEgoDownloadTests(unittest.TestCase):
    def test_only_successfully_downloaded_media_gets_an_ownership_receipt(self):
        payload = b"declared inert dataset source"
        resource = Mock()
        resource.meta.client.head_object.return_value = {"ContentLength": len(payload)}
        def download(_key, target, **options):
            Path(target).write_bytes(payload)
            options["Callback"](len(payload))
        resource.Bucket.return_value.download_file.side_effect = download
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "source.mp4"
            with patch.object(ego4d, "_s3", return_value=resource), \
                 patch.object(ego4d.shutil, "disk_usage", return_value=Disk(100, 0, 10**12)), \
                 patch.object(ego4d, "_record_acquired_media") as record:
                ego4d._download_to("fixture", "source", target, managed_role="source_video",
                                   validator=lambda path: path.read_bytes() == payload)
            record.assert_called_once_with(target, "source_video")
            self.assertEqual(target.read_bytes(), payload)
            self.assertFalse(list(target.parent.glob("*.part")))

    def test_existing_cache_and_catalog_are_not_claimed_as_managed_media(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "user-source.mp4"
            target.write_bytes(b"existing user material")
            with patch.object(ego4d, "_valid_mp4_cache", return_value=True), \
                 patch.object(ego4d, "_record_acquired_media") as record, \
                 patch.object(ego4d, "_download_to") as download:
                self.assertEqual(ego4d.download_clip({}, target), target)
            record.assert_not_called()
            download.assert_not_called()

    def test_insufficient_space_stops_before_download_and_preserves_destination(self):
        resource = Mock()
        resource.meta.client.head_object.return_value = {"ContentLength": 1024}
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "source.mp4"
            target.write_bytes(b"original")
            with patch.object(ego4d, "_s3", return_value=resource), \
                 patch.object(ego4d.shutil, "disk_usage", return_value=Disk(100, 99, 1)), \
                 patch.object(ego4d, "_record_acquired_media") as record:
                with self.assertRaisesRegex(OSError, "Espaço insuficiente"):
                    ego4d._download_to("fixture", "source", target, managed_role="source_video")
            resource.Bucket.return_value.download_file.assert_not_called()
            record.assert_not_called()
            self.assertEqual(target.read_bytes(), b"original")
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_runtime_space_exhaustion_never_publishes_partial_source(self):
        resource = Mock()
        resource.meta.client.head_object.return_value = {"ContentLength": 10}
        def download(_key, target, **options):
            Path(target).write_bytes(b"partial")
            options["Callback"](7)
        resource.Bucket.return_value.download_file.side_effect = download
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "source.mp4"
            with patch.object(ego4d, "_s3", return_value=resource), \
                 patch.object(ego4d.shutil, "disk_usage", side_effect=[Disk(100, 0, 10**12), Disk(100, 99, 1)]), \
                 patch.object(ego4d, "_record_acquired_media") as record:
                with self.assertRaisesRegex(OSError, "Espaço insuficiente"):
                    ego4d._download_to("fixture", "source", target, managed_role="source_video")
            self.assertFalse(target.exists())
            self.assertEqual(list(target.parent.iterdir()), [])
            record.assert_not_called()

    def test_stdlib_fallback_checks_advertised_size_before_reading_payload(self):
        response = io.BytesIO(b"declared inert payload")
        response.headers = {"Content-Length": "1000000"}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(ego4d, "_aws_creds", return_value=("fixture", "fixture", None)), \
             patch.object(ego4d, "_aws_region", return_value="us-east-1"), \
             patch.object(ego4d.tls, "urlopen", return_value=response), \
             patch.object(ego4d.shutil, "disk_usage", return_value=Disk(100, 99, 1)):
            target = Path(directory) / "download.part"
            with self.assertRaisesRegex(OSError, "Espaço insuficiente"):
                ego4d._s3_get_stdlib("fixture", "source", target, min_free_bytes=50)
            self.assertEqual(target.stat().st_size, 0)

    def test_receipt_contains_hash_of_actual_acquired_bytes(self):
        from moneymin import media_lifecycle
        payload = b"declared inert dataset source"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "source.mp4"
            target.write_bytes(payload)
            with patch.object(media_lifecycle, "record_managed_media") as record:
                ego4d._record_acquired_media(target, "source_video")
            self.assertEqual(record.call_args.kwargs["expected_digest"], hashlib.sha256(payload).hexdigest())
            self.assertEqual(record.call_args.kwargs["provider"], "ego4d")
            self.assertEqual(record.call_args.kwargs["root"], target.parent)
