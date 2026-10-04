"""Wire response failures stop transport and preserve a private diagnostic."""
from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from moneymin import upload, validate


class FakeSession:
    def __init__(self, create_body=None, sas_body=None, *, create_status=201, sas_status=200,
                 raw_create=None, raw_sas=None):
        self.create_body = {"id": "fixture-upload", "status": "initiated", "meta": {}} if create_body is None else create_body
        self.sas_body = sas_body
        self.create_status, self.sas_status = create_status, sas_status
        self.raw_create, self.raw_sas = raw_create, raw_sas
        self.events = []
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.startswith("/api/v1/uploads?"):
            self.events.append("create")
            return self.create_status, (self.raw_create if self.raw_create is not None
                                        else json.dumps(self.create_body))
        if path == "/api/v1/storage/sas/blobs":
            self.events.append("sas")
            response = self.sas_body if self.sas_body is not None else {
                "signed_urls": [{"filename": item["filename"],
                                 "blob_url": f"https://blob.invalid/{item['filename']}?sig=fixture-private-signature", 'expires_at': '2030-01-01T00:00:00Z'}
                                for item in body["files"]]}
            return self.sas_status, (self.raw_sas if self.raw_sas is not None else json.dumps(response))
        if path.endswith("/complete"):
            self.events.append("complete")
            return 200, json.dumps({'id': path.split('/')[-2], 'status': 'uploaded', 'meta': {}})
        if path.endswith("/fail"):
            self.events.append("fail")
            return 200, "{}"
        raise AssertionError(f"Unexpected fixture request: {method} {path}")


class UploadResponseContractTests(unittest.TestCase):
    MP4 = "fixture-session_0.mp4"
    ZIP = "fixture-session_0.data.zip"
    SECRET = "fixture-private-signature"

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.video = root / "video.mp4"
        self.video.write_bytes(b"fixture-video-content")
        self.stack.enter_context(patch.object(upload, "_probe_duration_ms", return_value=60_000))
        self.stack.enter_context(patch.object(upload.config, "recording_limits", return_value={
            "min_duration_ms": 60_000, "max_duration_ms": 1_800_000,
        }))
        # Local media checks have their own tests. These declared fixture bytes
        # isolate the server response contract without claiming valid Android media.
        self.stack.enter_context(patch.object(upload, "probe_video", return_value={}))
        self.stack.enter_context(patch.object(upload, "build_metadata_json", return_value={"source": "fixture"}))
        self.stack.enter_context(patch.object(upload, "_validate_sidecar_zip", return_value={"source": "fixture"}))
        self.stack.enter_context(patch.object(validate, "validate_upload_meta", return_value=[]))
        self.video_put = self.stack.enter_context(patch.object(upload, "_put_blob_file", return_value=201))
        self.zip_put = self.stack.enter_context(patch.object(upload, "_put_blob", return_value=201))
        self.checkpoint = Mock()

    def run_chunk(self, session, *, sidecar=False, register_first=None, fail_on_error=False):
        self.checkpoint.reset_mock()
        self.video_put.reset_mock()
        self.zip_put.reset_mock()
        self.video_put.side_effect = lambda *_args, **_kwargs: session.events.append("put-video") or 201
        self.zip_put.side_effect = lambda *_args, **_kwargs: session.events.append("put-sidecar") or 201
        kwargs = dict(session=session, video_path=self.video, org_key="fixture-org",
                      session_id="fixture-session", chunk_index=0, task_id="fixture-task",
                      content_type="video/mp4", timeout_blob=1,
                      recorded_at="2026-10-03T00:00:00.000Z", device_meta={},
                      platform_meta={}, video_meta={}, network_meta={},
                      max_retries=1, sidecar=sidecar,
                      sidecar_data=b"declared-fixture-sidecar" if sidecar else None,
                      fail_on_error=fail_on_error, checkpoint=self.checkpoint)
        if register_first is not None:
            kwargs["register_first"] = register_first
        return upload._upload_single_chunk(**kwargs)

    def assert_failed_before_transport(self, result, session, phase):
        self.assertEqual(result.state, upload.STATE_FAILED)
        self.assertIsInstance(result.error, str)
        self.assertNotIn(self.SECRET, result.error)
        self.assertNotIn("https://", result.error)
        self.video_put.assert_not_called()
        self.zip_put.assert_not_called()
        self.assertNotIn("complete", session.events)
        self.assertEqual(self.checkpoint.call_args.kwargs["phase"], phase)
        self.assertEqual(self.checkpoint.call_args.kwargs["state"], upload.STATE_FAILED)
        self.assertNotIn(self.SECRET, str(self.checkpoint.call_args))

    def test_new_upload_defaults_register_before_sas_and_transport(self):
        session = FakeSession()
        result = self.run_chunk(session)
        self.assertEqual(result.state, upload.STATE_DONE)
        self.assertEqual(session.events, ["create", "sas", "put-video", "complete"])
        self.assertIs(inspect.signature(upload.upload_session).parameters["register_first"].default, True)
        self.assertIs(inspect.signature(upload._upload_single_chunk).parameters["register_first"].default, True)

    def test_explicit_legacy_order_remains_recoverable(self):
        session = FakeSession()
        result = self.run_chunk(session, register_first=False)
        self.assertEqual(result.state, upload.STATE_DONE)
        self.assertEqual(session.events, ["sas", "put-video", "create", "complete"])

    def test_malformed_create_shapes_stop_before_sas(self):
        for value in (None, [], "fixture", 5, True):
            with self.subTest(value=value):
                session = FakeSession(raw_create=json.dumps(value))
                result = self.run_chunk(session)
                self.assert_failed_before_transport(result, session, "create")
                self.assertEqual(session.events, ["create"])

    def test_invalid_create_ids_do_not_get_coerced(self):
        values = (None, "", "  ", " fixture ", True, 8, 8.2, {}, [],
                  "fixture/upload", "fixture\\upload", "fixture?private", "fixture#private",
                  "fixture\nprivate", "fixture\x00private", ".", "..", "%2fprivate")
        for value in values:
            with self.subTest(value=value):
                session = FakeSession(create_body={"id": value})
                result = self.run_chunk(session)
                self.assert_failed_before_transport(result, session, "create")
                self.assertEqual(session.events, ["create"])

    def test_native_create_keeps_canonical_id_with_consistent_aliases(self):
        for key in ("id", "uploadId", "upload_id"):
            with self.subTest(key=key):
                session = FakeSession(create_body={"id": "fixture-upload", "status": "initiated", "meta": {}, key: "fixture-upload"})
                result = self.run_chunk(session)
                self.assertEqual(result.state, upload.STATE_DONE)
                self.assertEqual(result.upload_id, "fixture-upload")

    def test_explicit_legacy_order_keeps_alias_only_receipts(self):
        for key in ("id", "uploadId", "upload_id"):
            with self.subTest(key=key):
                session = FakeSession(create_body={key: "fixture-upload"})
                result = self.run_chunk(session, register_first=False)
                self.assertEqual(result.state, upload.STATE_DONE)
                self.assertEqual(result.upload_id, "fixture-upload")
                self.assertEqual(session.events, ["sas", "put-video", "create", "complete"])

    def test_native_alias_only_receipt_requires_review_before_sas(self):
        for key in ("uploadId", "upload_id"):
            with self.subTest(key=key):
                session = FakeSession(create_body={key: "fixture-upload", "status": "initiated", "meta": {}})
                result = self.run_chunk(session)
                self.assertEqual(result.state, upload.STATE_QUARANTINE)
                self.assertEqual(result.upload_id, "fixture-upload")
                self.assertEqual(self.checkpoint.call_args.kwargs["phase"], "registration_review")
                self.assertEqual(session.events, ["create"])
                self.video_put.assert_not_called()

    def test_conflicting_create_aliases_are_rejected(self):
        session = FakeSession(create_body={"id": "fixture-upload", "upload_id": "fixture-other"})
        self.assert_failed_before_transport(self.run_chunk(session), session, "create")

    def test_non_json_create_does_not_echo_private_body(self):
        session = FakeSession(raw_create=f"private response https://blob.invalid/video?sig={self.SECRET}")
        self.assert_failed_before_transport(self.run_chunk(session), session, "create")

    def test_http_create_failure_is_private_and_stops_before_sas(self):
        session = FakeSession(create_status=403, create_body={"detail": f"https://blob.invalid/a?sig={self.SECRET}"})
        self.assert_failed_before_transport(self.run_chunk(session), session, "create")
        self.assertEqual(session.events, ["create"])

    def test_completed_conflict_is_not_delivery_confirmation(self):
        session = FakeSession(create_status=409, create_body={
            "upload_id": "fixture-upload", "status": "completed"})
        result = self.run_chunk(session)
        self.assert_failed_before_transport(result, session, "create")
        self.assertEqual(session.events, ["create"])

    def test_canonical_uploaded_conflict_only_completes(self):
        session = FakeSession(create_status=409, create_body={
            "upload_id": "fixture-upload", "status": "uploaded"})
        result = self.run_chunk(session)
        self.assertEqual(result.state, upload.STATE_DONE)
        self.assertEqual(session.events, ["create", "complete"])
        self.video_put.assert_not_called()

    def test_canonical_initiated_conflict_runs_transport_with_same_identity(self):
        session = FakeSession(create_status=409, create_body={
            "upload_id": "fixture-upload", "status": "initiated"})
        result = self.run_chunk(session)
        self.assertEqual(result.state, upload.STATE_DONE)
        self.assertEqual(result.upload_id, "fixture-upload")
        self.assertEqual(session.events, ["create", "sas", "put-video", "complete"])

    def test_conflict_invalid_ids_and_divergent_nested_ids_are_rejected(self):
        bodies = [{"detail": {"id": value, "status": "completed"}}
                  for value in (True, 12, 12.5, {}, [], "fixture/path")]
        bodies.extend(({"id": "fixture-a", "detail": {"id": "fixture-b"}},
                       {"detail": "fixture-private-signature"}))
        for body in bodies:
            with self.subTest(body=body):
                session = FakeSession(create_status=409, create_body=body)
                self.assert_failed_before_transport(self.run_chunk(session), session, "create")

    def test_sas_invalid_shapes_stop_before_transport(self):
        values = (None, [], "fixture", True, 5, {}, {"signed_urls": None},
                  {"signed_urls": {}}, {"signed_urls": "fixture"}, {"signed_urls": []})
        for value in values:
            with self.subTest(value=value):
                session = FakeSession(raw_sas=json.dumps(value))
                self.assert_failed_before_transport(self.run_chunk(session), session, "sas")
                self.assertEqual(session.events, ["create", "sas"])

    def test_sas_invalid_entries_stop_before_transport(self):
        entries = (None, [], "fixture", 3, {}, {"filename": self.MP4},
                   {"filename": self.MP4, "blob_url": None, 'expires_at': '2030-01-01T00:00:00Z'},
                   {"filename": [], "blob_url": "https://blob.invalid/video", 'expires_at': '2030-01-01T00:00:00Z'},
                   {"filename": self.MP4, "blob_url": [], 'expires_at': '2030-01-01T00:00:00Z'},
                   {"filename": "fixture-other.mp4", "blob_url": "https://blob.invalid/video", 'expires_at': '2030-01-01T00:00:00Z'})
        for entry in entries:
            with self.subTest(entry=entry):
                session = FakeSession(sas_body={"signed_urls": [entry]})
                self.assert_failed_before_transport(self.run_chunk(session), session, "sas")

    def test_duplicate_filename_is_rejected_even_for_identical_urls(self):
        first = {"filename": self.MP4, "blob_url": "https://blob.invalid/video", 'expires_at': '2030-01-01T00:00:00Z'}
        for second in (first, {**first, "blob_url": "https://blob.invalid/other"}):
            with self.subTest(second=second):
                session = FakeSession(sas_body={"signed_urls": [first, second]})
                self.assert_failed_before_transport(self.run_chunk(session), session, "sas")

    def test_missing_requested_zip_stops_before_mp4_put(self):
        session = FakeSession(sas_body={"signed_urls": [{
            "filename": self.MP4, "blob_url": "https://blob.invalid/video", 'expires_at': '2030-01-01T00:00:00Z'}]})
        self.assert_failed_before_transport(self.run_chunk(session, sidecar=True), session, "sas")

    def test_missing_requested_mp4_stops_before_zip_put(self):
        session = FakeSession(sas_body={"signed_urls": [{
            "filename": self.ZIP, "blob_url": "https://blob.invalid/sidecar", 'expires_at': '2030-01-01T00:00:00Z'}]})
        self.assert_failed_before_transport(self.run_chunk(session, sidecar=True), session, "sas")

    def test_mp4_and_zip_cannot_overwrite_the_same_blob(self):
        for host in ("blob.invalid", "blob.invalid:443"):
            with self.subTest(host=host):
                session = FakeSession(sas_body={"signed_urls": [
                    {"filename": self.MP4, "blob_url": "https://blob.invalid/same-resource?sig=fixture-a", 'expires_at': '2030-01-01T00:00:00Z'},
                    {"filename": self.ZIP, "blob_url": f"https://{host}/same-resource?sig=fixture-b", 'expires_at': '2030-01-01T00:00:00Z'}]})
                self.assert_failed_before_transport(self.run_chunk(session, sidecar=True), session, "sas")

    def test_sas_url_must_be_safe_absolute_https_without_echo(self):
        urls = ("", " /video ", "file:///fixture/private", "http://blob.invalid/video",
                "https:///video", "https://blob.invalid", "https://blob.invalid/",
                "https://user:fixture-private-signature@blob.invalid/video",
                "https://blob.invalid/video#fixture-private-signature",
                "https://blob.invalid/video?sig=fixture-private-signature\nprivate",
                "https://blob.invalid\\other/video?sig=fixture-private-signature",
                "https://blob.invalid:0/video", "https://blob.invalid:invalid/video")
        for url in urls:
            with self.subTest(url=url):
                session = FakeSession(sas_body={"signed_urls": [{"filename": self.MP4, "blob_url": url, 'expires_at': '2030-01-01T00:00:00Z'}]})
                self.assert_failed_before_transport(self.run_chunk(session), session, "sas")

    def test_valid_mp4_zip_preserve_signed_queries_and_upload_once(self):
        mp4_url = f"https://blob.invalid/video?sig={self.SECRET}&sp=cw&se=fixture"
        zip_url = f"https://blob.invalid/sidecar?sig={self.SECRET}&sp=cw&se=fixture"
        session = FakeSession(sas_body={"signed_urls": [
            {"filename": self.MP4, "blob_url": mp4_url, 'expires_at': '2030-01-01T00:00:00Z'},
            {"filename": self.ZIP, "blob_url": zip_url, 'expires_at': '2030-01-01T00:00:00Z'}]})
        result = self.run_chunk(session, sidecar=True)
        self.assertEqual(result.state, upload.STATE_DONE)
        self.assertEqual(session.events, ["create", "sas", "put-video", "put-sidecar", "complete"])
        self.video_put.assert_called_once()
        self.zip_put.assert_called_once()
        self.assertEqual(self.video_put.call_args.args[0], mp4_url)
        self.assertEqual(self.zip_put.call_args.args[0], zip_url)
        self.assertEqual(result.blob_path, "video")
        self.assertEqual(result.sidecar_blob_path, "sidecar")

    def test_non_json_sas_does_not_echo_private_body(self):
        session = FakeSession(raw_sas=f"private response https://blob.invalid/video?sig={self.SECRET}")
        self.assert_failed_before_transport(self.run_chunk(session), session, "sas")

    def test_http_sas_failure_preserves_status_but_not_response_credentials(self):
        session = FakeSession(sas_status=403, sas_body={"detail": f"https://blob.invalid/video?sig={self.SECRET}"})
        self.assert_failed_before_transport(self.run_chunk(session), session, "sas")
        for status in (429, 500):
            error = upload._http_upload_error("SAS", status, self.SECRET)
            self.assertEqual(error.status_code, status)
            self.assertTrue(error.retryable)
            self.assertNotIn(self.SECRET, str(error))
        for phase in ("SAS", "POST /uploads"):
            with self.subTest(phase=phase):
                error = upload._http_upload_error(phase, 403, self.SECRET, {
                    "X-Blocked-Reason": f"https://blob.invalid/video?sig={self.SECRET}"})
                self.assertNotIn(self.SECRET, str(error))
                self.assertNotIn("https://", str(error))

    def test_sas_malformed_response_marks_existing_upload_failed_without_credentials(self):
        session = FakeSession(raw_sas=f"https://blob.invalid/video?sig={self.SECRET}")
        self.assert_failed_before_transport(self.run_chunk(session, fail_on_error=True), session, "sas")
        self.assertEqual(session.events, ["create", "sas", "fail"])
        failure_body = session.calls[-1][2]
        self.assertNotIn(self.SECRET, json.dumps(failure_body))

    def test_non_boolean_order_policy_stops_before_remote_effects(self):
        for value in ("false", 0, 1, [], {}):
            with self.subTest(value=value):
                session = FakeSession()
                self.assert_failed_before_transport(self.run_chunk(session, register_first=value), session, "preflight")
                self.assertEqual(session.events, [])
                with self.assertRaises(upload.UploadError):
                    upload.upload_session(session, self.video, "fixture-org", register_first=value)
