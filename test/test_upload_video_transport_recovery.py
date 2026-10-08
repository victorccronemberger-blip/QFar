"""Resume an accepted video receipt with its exact persisted MP4 and ZIP."""
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from moneymin import device_profile, upload


class VideoTransportRecoveryTests(unittest.TestCase):
    OWNER = "fixture@example.invalid"
    SID = "known-video-receipt"
    UPLOAD_ID = "known-upload-id"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qmoney-video-recovery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journals = self.root / "sidecars"
        self.journals.mkdir()
        self.video = self.root / "original.mp4"
        self.video_bytes = b"declared inert original MP4 bytes"
        self.video.write_bytes(self.video_bytes)
        self.stack = patch.object(upload, "sidecars_dir", return_value=self.journals)
        self.stack.start()
        self.addCleanup(self.stack.stop)
        self.recorded_at = device_profile.format_recorded_at(time.time() - 1200)
        self.profile = device_profile._create_profile(self.OWNER)
        self.zip_bytes = upload.build_sidecar_zip(
            session_id=self.SID, chunk_index=0, duration_ms=60_000,
            recorded_at=self.recorded_at,
            video_probe={"duration_ms": 60_000, "fps": 30, "width": 1440,
                         "height": 1080, "codec": "h264", "has_video": True,
                         "has_audio": True},
            device_meta=self.profile.sidecar_device_meta(),
            platform_meta=self.profile.sidecar_platform_meta(),
            calib=self.profile.calib, uptime_ns=200_000_000_000_000,
            frames_gop=self.profile.frames_gop, imu_seed=self.profile.device_id,
        )
        self.archive = upload._sidecar_archive_path(self.SID, 0)
        self.archive.parent.mkdir(parents=True, exist_ok=True)
        self.archive.write_bytes(self.zip_bytes)
        self.row = {
            "schema_version": 2,
            "session_id": self.SID, "chunk_index": 0,
            "expected_chunk_count": 1, "account_email": self.OWNER,
            "org_key": "fixture-org", "task_id": "fixture-task",
            "upload_id": self.UPLOAD_ID, "state": upload.STATE_FAILED,
            "phase": "transport", "error": "PUT Blob falhou (6): curl: (6) DNS fixture",
            "create_attempted": True, "register_first": True,
            "native_response_schema": True, "suppress_per_chunk_catbear": True,
            "conflict_action": "",
            "finalize_requested": True, "finalized": False,
            "evaluation_required": True, "evaluation_verified": False,
            "recorded_at": self.recorded_at,
            "log_id": f"{self.SID}_0", "filename": f"{self.SID}_0.mp4",
            "local_video_path": str(self.video.resolve()),
            "size_bytes": len(self.video_bytes), "duration_ms": 60_000,
            "video_content_sha256": hashlib.sha256(self.video_bytes).hexdigest(),
            "sidecar_data_path": str(self.archive.resolve()),
            "sidecar_size_bytes": len(self.zip_bytes),
            "sidecar_sha256": hashlib.sha256(self.zip_bytes).hexdigest(),
            "attempts": 1, "crash_resumes": 0,
            # The 2.0.79 journal predates transport_artifact on this path.
        }
        self.session_calls = []

        class Session:
            email = VideoTransportRecoveryTests.OWNER

            def request(inner, method, path, body=None):
                self.session_calls.append((method, path, body))
                if path == "/api/v1/storage/sas/blobs":
                    return 200, json.dumps({"signed_urls": [
                        {"filename": item["filename"],
                         "blob_url": f"https://blob.invalid/{item['filename']}?sig=fixture",
                         "expires_at": "2030-01-01T00:00:00Z"}
                        for item in body["files"]]})
                if path.endswith("/complete"):
                    return 200, json.dumps({"id": VideoTransportRecoveryTests.UPLOAD_ID,
                                            "status": "uploaded", "meta": {}})
                if path.endswith("/evaluate"):
                    return 200, json.dumps({
                        "upload_id": VideoTransportRecoveryTests.UPLOAD_ID,
                        "checks": [{"id": "fixture-quality", "label": "Fixture quality",
                                    "status": "pass", "detail": None}]})
                if path.endswith("/finalize"):
                    return 204, ""
                raise AssertionError(f"Unexpected service call: {method} {path}")

        self.session = Session()

    def persist(self, **changes):
        row = {**self.row, **changes}
        return upload.save_sidecar(row)

    def test_failed_curl_dns_is_selected_and_resumes_same_video_and_zip_receipt(self):
        self.persist()
        video_puts, zip_puts = [], []

        def put_video(url, path, **kwargs):
            video_puts.append((url, Path(path).read_bytes(), kwargs.get("expected_sha256")))
            return 201

        def put_zip(url, payload, **_kwargs):
            zip_puts.append((url, payload))
            return 201

        with patch.object(device_profile, "_load_profile", return_value=self.profile), \
             patch.object(upload, "_put_blob_file", side_effect=put_video), \
             patch.object(upload, "_put_blob", side_effect=put_zip):
            self.assertTrue(upload.is_pending_transport(self.row))
            rows = upload.pump_pending(self.session, max_retries=1, retry_backoff=0,
                                       fail_on_error=False, profile=self.profile)

        self.assertEqual([path for _method, path, _body in self.session_calls], [
            "/api/v1/storage/sas/blobs",
            f"/api/v1/uploads/{self.UPLOAD_ID}/complete",
            f"/api/v1/uploads/{self.UPLOAD_ID}/evaluate",
            f"/api/v1/organizations/fixture-org/sessions/{self.SID}/finalize",
        ])
        sas_body = self.session_calls[0][2]
        self.assertEqual({row["filename"] for row in sas_body["files"]}, {
            f"{self.SID}_0.mp4", f"{self.SID}_0.data.zip"})
        self.assertEqual(video_puts, [(video_puts[0][0], self.video_bytes,
                                      self.row["video_content_sha256"])])
        self.assertEqual(zip_puts[0][1], self.zip_bytes)
        self.assertEqual(rows[0]["upload_id"], self.UPLOAD_ID)
        self.assertEqual(rows[0]["recorded_at"], self.recorded_at)
        self.assertIs(rows[0]["finalized"], True)

    def test_owner_profile_hash_zip_time_and_phase_guards_fail_before_effects(self):
        mutations = [
            ("profile-owner", {"profile": device_profile._create_profile("other@example.invalid")}),
            ("video-bytes", {"video_bytes": b"changed"}),
            ("zip-bytes", {"zip_bytes": b"changed"}),
            ("recorded-at", {"recorded_at": "2026-10-01T00:00:00.000Z"}),
            ("phase", {"phase": "sas_ready"}),
            ("permanent-failure", {"error": "PUT Blob falhou (403): forbidden"}),
        ]
        for label, changes in mutations:
            with self.subTest(label=label):
                self.session_calls.clear()
                self.video.write_bytes(self.video_bytes)
                self.archive.write_bytes(self.zip_bytes)
                candidate = {**self.row}
                profile = self.profile
                if "profile" in changes:
                    profile = changes["profile"]
                elif "video_bytes" in changes:
                    self.video.write_bytes(changes["video_bytes"])
                elif "zip_bytes" in changes:
                    self.archive.write_bytes(changes["zip_bytes"])
                elif "recorded_at" in changes:
                    candidate["recorded_at"] = changes["recorded_at"]
                elif "phase" in changes:
                    candidate["phase"] = changes["phase"]
                elif "error" in changes:
                    candidate["error"] = changes["error"]
                before_video, before_zip = self.video.read_bytes(), self.archive.read_bytes()
                with patch.object(upload, "list_sidecars", return_value=[candidate]), \
                     patch.object(upload, "save_sidecar") as save, \
                     patch.object(upload, "_put_blob_file") as put_video, \
                     patch.object(upload, "_put_blob") as put_zip, \
                     patch.object(device_profile, "_load_profile", return_value=profile):
                    with self.assertRaises(upload.UploadError):
                        upload.pump_pending(self.session, state=upload.STATE_FAILED,
                                            max_retries=1,
                                            retry_backoff=0, fail_on_error=False)
                save.assert_not_called()
                put_video.assert_not_called()
                put_zip.assert_not_called()
                self.assertEqual(self.session_calls, [])
                self.assertEqual(self.video.read_bytes(), before_video)
                self.assertEqual(self.archive.read_bytes(), before_zip)

    def test_sidecar_only_transport_resume_does_not_read_or_put_mp4(self):
        row = {**self.row, "transport_artifact": "sidecar", "conflict_action": "complete",
               "local_video_path": str(self.root / "already-removed.mp4")}
        self.persist(**{key: value for key, value in row.items() if key != "session_id"})
        zip_puts = []

        with patch.object(upload, "_put_blob_file") as put_video, \
             patch.object(upload, "_put_blob", side_effect=lambda url, payload, **kwargs:
                          zip_puts.append(payload) or 201):
            rows = upload.pump_pending(self.session, max_retries=1, retry_backoff=0,
                                       fail_on_error=False)

        put_video.assert_not_called()
        self.assertEqual(zip_puts, [self.zip_bytes])
        self.assertEqual(rows[0]["upload_id"], self.UPLOAD_ID)
        self.assertIs(rows[0]["finalized"], True)

    def test_transient_sas_failure_resumes_existing_video_receipt(self):
        self.persist()
        original_request = self.session.request
        failed_sas = False

        def fail_first_sas(method, path, body=None):
            nonlocal failed_sas
            if path == "/api/v1/storage/sas/blobs":
                if not failed_sas:
                    failed_sas = True
                    self.session_calls.append((method, path, body))
                    return 503, '{"message":"temporary fixture outage"}'
            return original_request(method, path, body)

        video_puts, zip_puts = [], []
        with patch.object(self.session, "request", side_effect=fail_first_sas), \
             patch.object(device_profile, "_load_profile", return_value=self.profile), \
             patch.object(upload, "_put_blob_file", side_effect=lambda url, path, **kw:
                          video_puts.append(Path(path).read_bytes()) or 201), \
             patch.object(upload, "_put_blob", side_effect=lambda url, payload, **kw:
                          zip_puts.append(payload) or 201):
            first = upload.pump_pending(self.session, max_retries=1, retry_backoff=0,
                                        fail_on_error=False)
            self.assertEqual(first[0]["state"], upload.STATE_RETRY_LATE)
            self.assertEqual(first[0]["phase"], "sas")
            self.assertEqual(first[0]["transport_artifact"], "video")
            self.assertTrue(upload.is_pending_transport(first[0]))

            second = upload.pump_pending(self.session, max_retries=1, retry_backoff=0,
                                         fail_on_error=False)

        self.assertEqual(video_puts, [self.video_bytes])
        self.assertEqual(zip_puts, [self.zip_bytes])
        self.assertEqual(second[0]["upload_id"], self.UPLOAD_ID)
        self.assertIs(second[0]["finalized"], True)
        self.assertFalse(any(method == "POST" and path == "/api/v1/uploads"
                             for method, path, _body in self.session_calls))


if __name__ == "__main__":
    unittest.main()
