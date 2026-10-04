"""Local completion policy: new sends, explicit legacy opt-out and recovery."""

from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import time
import unittest
from contextlib import ExitStack, contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from moneymin import device_profile, upload
from moneymin.upload_types import ChunkResult, UploadResult


class _Session:
    email = "fixture@example.invalid"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.complete_bodies: list[dict] = []
        self.created = 0

    def ensure_auth(self) -> dict:
        return {"email": self.email}

    def request(self, method: str, path: str, body=None):
        self.calls.append((method, path, body))
        if path == "/api/v1/storage/sas/blobs":
            return 200, json.dumps({"signed_urls": [
                {"filename": item["filename"],
                 "blob_url": f"https://blob.invalid/{item['filename']}", 'expires_at': '2030-01-01T00:00:00Z'}
                for item in body["files"]]})
        if path.startswith("/api/v1/uploads?"):
            self.created += 1
            return 201, json.dumps({"id": f"fixture-upload-{self.created}", "status": "initiated", "meta": {}})
        if path.endswith("/complete"):
            self.complete_bodies.append(dict(body))
            return 200, json.dumps({'id': path.split('/')[-2], 'status': 'uploaded', 'meta': {}})
        if path.endswith("/finalize"):
            return 204, ""
        raise AssertionError(f"Unexpected fixture operation: {method} {path}")


@contextmanager
def _fixture(count: int = 1):
    with tempfile.TemporaryDirectory(prefix="qmoney-completion-policy-") as temporary:
        root = Path(temporary)
        journals = root / "journals"
        paths = [root / f"chunk{index}.mp4" for index in range(count)]
        for path in paths:
            path.write_bytes(b"local fixture, not a real media recording")
        recorded = device_profile.format_recorded_at(time.time() - (count + 1) * 60)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(upload, "sidecars_dir", return_value=journals))
            stack.enter_context(mock.patch.object(upload, "_probe_duration_ms", return_value=60_000))
            stack.enter_context(mock.patch.object(upload, "_put_blob_file", return_value=201))
            stack.enter_context(mock.patch.object(upload, "_put_blob", return_value=201))
            stack.enter_context(mock.patch.object(upload, "probe_video", return_value={
                "duration_ms": 60_000, "fps": 30.0, "width": 1440,
                "height": 1080, "codec": "h264", "has_video": True,
                "has_audio": True}))
            yield root, paths, recorded


def _send(session, paths, recorded=None, **options):
    arguments = dict(session_id="fixture-session", task_id="fixture-task",
                     normalize=False, sidecar=False, finalize=False,
                     persist_sidecar=True, max_retries=1)
    if recorded is not None:
        arguments["recorded_at"] = recorded
    arguments.update(options)
    return upload.upload_session(session, paths, "fixture-org", **arguments)


def _journal(path, recorded, index=0, **overrides):
    row = dict(session_id="fixture-session", org_key="fixture-org",
               task_id="fixture-task", chunk_index=index,
               log_id=f"fixture-session_{index}", filename=f"fixture-session_{index}.mp4",
               account_email=_Session.email, recorded_at=recorded,
               local_video_path=str(path.resolve()), size_bytes=path.stat().st_size,
               state=upload.STATE_CREATING, phase="queued", attempts=0,
               crash_resumes=0, expected_chunk_count=1, finalize_requested=False,
               register_first=True)
    row.update(overrides)
    upload.save_sidecar(row)
    return row


class CompletionPolicyTests(unittest.TestCase):
    def test_complete_helper_defaults_to_explicit_native_true(self):
        session = _Session()
        upload.complete_upload(session, "fixture-upload", 12)
        self.assertIs(session.complete_bodies[0].get("suppress_per_chunk_catbear"), True)
        self.assertNotIn("session_complete", session.complete_bodies[0])

    def test_direct_chunk_defaults_to_true(self):
        with _fixture() as (_, paths, recorded):
            session = _Session()
            result = upload._upload_single_chunk(
                session, paths[0], "fixture-org", "fixture-session", 0,
                "fixture-task", "video/mp4", 300, recorded,
                None, None, None, None, sidecar=False, max_retries=1)
        self.assertEqual(result.state, upload.STATE_DONE)
        self.assertIs(session.complete_bodies[0].get("suppress_per_chunk_catbear"), True)

    def test_new_single_chunk_wire_and_journal_use_true(self):
        with _fixture() as (_, paths, recorded):
            session = _Session()
            _send(session, paths, recorded)
            row = upload.load_sidecar("fixture-session", 0)
        self.assertIs(session.complete_bodies[0].get("suppress_per_chunk_catbear"), True)
        self.assertIs(row["suppress_per_chunk_catbear"], True)

    def test_new_multi_chunk_wire_and_journals_use_true(self):
        with _fixture(2) as (_, paths, recorded):
            session = _Session()
            _send(session, paths, recorded)
            rows = [upload.load_sidecar("fixture-session", index) for index in range(2)]
        self.assertEqual(len(session.complete_bodies), 2)
        self.assertTrue(all(body.get("suppress_per_chunk_catbear") is True
                            for body in session.complete_bodies))
        self.assertTrue(all(row["suppress_per_chunk_catbear"] is True for row in rows))

    def test_explicit_false_is_historical_omission_for_any_chunk_count(self):
        for count in (1, 2):
            with self.subTest(count=count), _fixture(count) as (_, paths, recorded):
                session = _Session()
                _send(session, paths, recorded, suppress_per_chunk_catbear=False)
                self.assertTrue(all("suppress_per_chunk_catbear" not in body
                                    for body in session.complete_bodies))
                self.assertTrue(all(upload.load_sidecar("fixture-session", index)
                                    ["suppress_per_chunk_catbear"] is False
                                    for index in range(count)))

    def test_journal_false_governs_conflicting_live_true(self):
        with _fixture() as (_, paths, recorded):
            _journal(paths[0], recorded, suppress_per_chunk_catbear=False)
            session = _Session()
            _send(session, paths, recorded, suppress_per_chunk_catbear=True)
            row = upload.load_sidecar("fixture-session", 0)
        self.assertNotIn("suppress_per_chunk_catbear", session.complete_bodies[0])
        self.assertIs(row["suppress_per_chunk_catbear"], False)

    def test_journal_true_governs_conflicting_live_false(self):
        with _fixture() as (_, paths, recorded):
            _journal(paths[0], recorded, suppress_per_chunk_catbear=True)
            session = _Session()
            _send(session, paths, recorded, suppress_per_chunk_catbear=False)
            row = upload.load_sidecar("fixture-session", 0)
        self.assertIs(session.complete_bodies[0].get("suppress_per_chunk_catbear"), True)
        self.assertIs(row["suppress_per_chunk_catbear"], True)

    def test_legacy_missing_flag_keeps_omission_without_migration(self):
        with _fixture() as (_, paths, recorded):
            _journal(paths[0], recorded)
            session = _Session()
            _send(session, paths, recorded, suppress_per_chunk_catbear=True)
            row = upload.load_sidecar("fixture-session", 0)
        self.assertNotIn("suppress_per_chunk_catbear", session.complete_bodies[0])
        self.assertNotIn("suppress_per_chunk_catbear", row)

    def test_existing_recording_time_and_identity_are_preserved(self):
        with _fixture() as (_, paths, recorded):
            original = _journal(paths[0], recorded, suppress_per_chunk_catbear=False)
            session = _Session()
            _send(session, paths)
            row = upload.load_sidecar("fixture-session", 0)
        for field in ("session_id", "chunk_index", "log_id", "filename", "account_email",
                      "org_key", "task_id", "recorded_at"):
            self.assertEqual(row[field], original[field], field)
        create = next(body for _, path, body in session.calls
                      if path.startswith("/api/v1/uploads?"))
        self.assertEqual(create["recorded_at"], original["recorded_at"])

    def test_complete_rejects_non_boolean_flags_before_http(self):
        for field in ("suppress_per_chunk_catbear", "session_complete"):
            for invalid in (None, 0, 1, "false", "true", [], {}):
                with self.subTest(field=field, invalid=invalid):
                    session = _Session()
                    with self.assertRaises(upload.UploadError) as caught:
                        upload.complete_upload(session, "fixture-upload", 12, **{field: invalid})
                    self.assertFalse(caught.exception.retryable)
                    self.assertEqual(session.calls, [])

    def test_session_rejects_non_boolean_flag_before_normalization_or_journal(self):
        for invalid in (None, 0, 1, "false", "true", [], {}):
            with self.subTest(invalid=invalid), _fixture() as (root, paths, recorded):
                session = _Session()
                with mock.patch.object(upload, "normalize_video", side_effect=lambda path: path) as normalize:
                    with self.assertRaises(upload.UploadError) as caught:
                        _send(session, paths, recorded, normalize=True,
                              suppress_per_chunk_catbear=invalid)
                normalize.assert_not_called()
                self.assertEqual(session.calls, [])
                self.assertFalse((root / "journals").exists())
                self.assertFalse(caught.exception.retryable)

    def test_invalid_later_journal_blocks_entire_session_before_effects(self):
        with _fixture(2) as (_, paths, recorded):
            first = _journal(paths[0], recorded, expected_chunk_count=2,
                             suppress_per_chunk_catbear=False)
            _journal(paths[1], device_profile.format_recorded_at(
                device_profile.recorded_at_to_wall_ms(recorded) / 1000 + 60),
                index=1, expected_chunk_count=2, suppress_per_chunk_catbear="false")
            files = [upload._sidecar_path("fixture-session", index) for index in range(2)]
            before = [path.read_bytes() for path in files]
            session = _Session()
            with mock.patch.object(upload, "normalize_video", side_effect=lambda path: path) as normalize:
                with self.assertRaises(upload.UploadError):
                    _send(session, paths, recorded, normalize=True)
            normalize.assert_not_called()
            self.assertEqual(session.calls, [])
            self.assertEqual([path.read_bytes() for path in files], before)

    def test_resume_complete_retains_false_true_and_missing_policy(self):
        for flag in (False, True, "missing"):
            with self.subTest(flag=flag), _fixture() as (_, paths, recorded):
                options = {} if flag == "missing" else {"suppress_per_chunk_catbear": flag}
                _journal(paths[0], recorded, phase="transport_done",
                         state=upload.STATE_TRANSPORT, upload_id="fixture-upload", **options)
                session = _Session()
                upload.pump_pending(session, max_retries=1)
                row = upload.load_sidecar("fixture-session", 0)
                if flag is True:
                    self.assertIs(session.complete_bodies[0].get("suppress_per_chunk_catbear"), True)
                else:
                    self.assertNotIn("suppress_per_chunk_catbear", session.complete_bodies[0])
                self.assertEqual(row.get("suppress_per_chunk_catbear", "missing"), flag)
                self.assertEqual(session.created, 0)

    def test_resume_pretransport_retains_false_true_and_missing_policy(self):
        for flag in (False, True, "missing"):
            with self.subTest(flag=flag), _fixture() as (_, paths, recorded):
                options = {} if flag == "missing" else {"suppress_per_chunk_catbear": flag}
                _journal(paths[0], recorded, **options)
                session = _Session()
                upload.pump_pending(session, max_retries=1)
                row = upload.load_sidecar("fixture-session", 0)
                if flag is True:
                    self.assertIs(session.complete_bodies[0].get("suppress_per_chunk_catbear"), True)
                else:
                    self.assertNotIn("suppress_per_chunk_catbear", session.complete_bodies[0])
                self.assertEqual(row.get("suppress_per_chunk_catbear", "missing"), flag)
                self.assertEqual(row["recorded_at"], recorded)

    def test_session_complete_preserves_historical_force_true_contract(self):
        session = _Session()
        upload.complete_upload(session, "fixture-upload", 12,
                               suppress_per_chunk_catbear=False, session_complete=True)
        self.assertIs(session.complete_bodies[0]["suppress_per_chunk_catbear"], True)
        self.assertIs(session.complete_bodies[0]["session_complete"], True)

    def test_journal_policy_is_honored_even_without_persistence_requested(self):
        with _fixture() as (_, paths, recorded):
            _journal(paths[0], recorded, suppress_per_chunk_catbear=False)
            target = upload._sidecar_path("fixture-session", 0)
            before = target.read_bytes()
            session = _Session()
            _send(session, paths, recorded, persist_sidecar=False,
                  suppress_per_chunk_catbear=True)
            self.assertEqual(target.read_bytes(), before)
        self.assertNotIn("suppress_per_chunk_catbear", session.complete_bodies[0])

    def test_mixed_existing_chunk_policies_govern_each_chunk(self):
        with _fixture(2) as (_, paths, recorded):
            second_time = device_profile.format_recorded_at(
                device_profile.recorded_at_to_wall_ms(recorded) / 1000 + 60)
            _journal(paths[0], recorded, expected_chunk_count=2,
                     suppress_per_chunk_catbear=False)
            _journal(paths[1], second_time, index=1, expected_chunk_count=2,
                     suppress_per_chunk_catbear=True)
            session = _Session()
            _send(session, paths, recorded, suppress_per_chunk_catbear=True)
        self.assertNotIn("suppress_per_chunk_catbear", session.complete_bodies[0])
        self.assertIs(session.complete_bodies[1]["suppress_per_chunk_catbear"], True)

    def test_direct_chunk_rejects_invalid_flags_before_probe_or_checkpoint(self):
        with _fixture() as (_, paths, recorded):
            for field in ("suppress_per_chunk_catbear", "session_complete"):
                for invalid in (None, 0, 1, "false", "true", [], {}):
                    with self.subTest(field=field, invalid=invalid):
                        session = _Session()
                        checkpoint = mock.Mock()
                        with mock.patch.object(upload, "_probe_duration_ms") as probe:
                            with self.assertRaises(upload.UploadError):
                                upload._upload_single_chunk(
                                    session, paths[0], "fixture-org", "fixture-session", 0,
                                    "fixture-task", "video/mp4", 300, recorded,
                                    None, None, None, None, sidecar=False,
                                    checkpoint=checkpoint, **{field: invalid})
                        probe.assert_not_called()
                        checkpoint.assert_not_called()
                        self.assertEqual(session.calls, [])

    def test_direct_chunk_order_error_keeps_failed_result_and_preflight_callback_contract(self):
        with _fixture() as (_, paths, recorded):
            session = _Session()
            checkpoint = mock.Mock()
            with mock.patch.object(upload, "_probe_duration_ms") as probe:
                result = upload._upload_single_chunk(
                    session, paths[0], "fixture-org", "fixture-session", 0,
                    "fixture-task", "video/mp4", 300, recorded,
                    None, None, None, None, sidecar=False,
                    checkpoint=checkpoint, register_first="false")
            self.assertEqual(result.state, upload.STATE_FAILED)
            checkpoint.assert_called_once_with(state=upload.STATE_FAILED, phase="preflight",
                                               error=result.error)
            probe.assert_not_called()
            self.assertEqual(session.calls, [])


class ExistingRecordingTimeTests(unittest.TestCase):
    def test_missing_or_invalid_existing_time_blocks_before_normalization_or_rewrite(self):
        for invalid in (None, "", "not-a-timestamp", 0, True, [], {}):
            with self.subTest(invalid=invalid), _fixture() as (_, paths, recorded):
                _journal(paths[0], invalid, suppress_per_chunk_catbear=False)
                target = upload._sidecar_path("fixture-session", 0)
                before = target.read_bytes()
                session = _Session()
                with mock.patch.object(upload, "normalize_video", side_effect=lambda path: path) as normalize:
                    with self.assertRaises(upload.UploadError) as caught:
                        _send(session, paths, recorded, normalize=True)
                normalize.assert_not_called()
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(target.read_bytes(), before)
                self.assertEqual(session.calls, [])

    def test_legacy_absent_time_is_not_invented(self):
        with _fixture() as (_, paths, recorded):
            row = _journal(paths[0], recorded, suppress_per_chunk_catbear=False)
            del row["recorded_at"]
            target = upload.save_sidecar(row)
            before = target.read_bytes()
            session = _Session()
            with self.assertRaises(upload.UploadError):
                _send(session, paths, recorded)
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(session.calls, [])

    def test_resume_invalid_recording_time_preserves_all_journals_before_checkpoint(self):
        with _fixture(2) as (_, paths, recorded):
            _journal(paths[0], recorded, suppress_per_chunk_catbear=False)
            _journal(paths[1], "invalid-fixture-time", index=1,
                     suppress_per_chunk_catbear=True)
            files = [upload._sidecar_path("fixture-session", index) for index in range(2)]
            before = [path.read_bytes() for path in files]
            session = _Session()
            with self.assertRaises(upload.UploadError):
                upload.pump_pending(session, max_retries=1)
            self.assertEqual([path.read_bytes() for path in files], before)
            self.assertEqual(session.calls, [])

    def test_existing_noncanonical_valid_time_is_preserved_exactly(self):
        with _fixture() as (_, paths, recorded):
            original_time = recorded.replace("Z", "+00:00")
            _journal(paths[0], original_time, suppress_per_chunk_catbear=False)
            session = _Session()
            _send(session, paths)
            row = upload.load_sidecar("fixture-session", 0)
        self.assertEqual(row["recorded_at"], original_time)
        create = next(body for _, path, body in session.calls
                      if path.startswith("/api/v1/uploads?"))
        self.assertEqual(create["recorded_at"], original_time)

    def test_conflicting_explicit_time_does_not_replace_journal(self):
        with _fixture() as (_, paths, recorded):
            _journal(paths[0], recorded, suppress_per_chunk_catbear=False)
            target = upload._sidecar_path("fixture-session", 0)
            before = target.read_bytes()
            session = _Session()
            different = device_profile.format_recorded_at(
                device_profile.recorded_at_to_wall_ms(recorded) / 1000 + 30)
            with self.assertRaises(upload.UploadError):
                _send(session, paths, different)
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(session.calls, [])


class ExistingReceiptAndOrderTests(unittest.TestCase):
    def test_known_remote_receipt_blocks_restage_before_any_probe_or_effect(self):
        for phase in ("registered", "sas_ready", "transport", "transport_done", "complete",
                      "awaiting_finalize", "done", "evaluation_review"):
            with self.subTest(phase=phase), _fixture() as (_, paths, recorded):
                _journal(paths[0], recorded, state=upload.STATE_TRANSPORT, phase=phase,
                         upload_id="original-fixture-receipt",
                         suppress_per_chunk_catbear=False)
                target = upload._sidecar_path("fixture-session", 0)
                before = target.read_bytes()
                session = _Session()
                with (mock.patch.object(upload, "_probe_duration_ms", return_value=60_000) as probe,
                      mock.patch.object(upload, "normalize_video", side_effect=lambda path: path) as normalize,
                      mock.patch.object(upload, "save_sidecar", wraps=upload.save_sidecar) as save):
                    with self.assertRaises(upload.UploadError) as caught:
                        _send(session, paths, recorded, normalize=True)
                probe.assert_not_called()
                normalize.assert_not_called()
                save.assert_not_called()
                self.assertEqual(target.read_bytes(), before)
                self.assertEqual(session.calls, [])
                self.assertFalse(caught.exception.retryable)
                self.assertIn("Retomada", str(caught.exception))

    def test_completed_status_without_receipt_does_not_become_new_send(self):
        with _fixture() as (_, paths, recorded):
            _journal(paths[0], recorded, state=upload.STATE_DONE, phase="done",
                     suppress_per_chunk_catbear=False)
            target = upload._sidecar_path("fixture-session", 0)
            before = target.read_bytes()
            session = _Session()
            with self.assertRaises(upload.UploadError):
                _send(session, paths, recorded)
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(session.calls, [])

    def test_unsupported_receipt_resume_is_preserved_before_checkpoint(self):
        with _fixture() as (_, paths, recorded):
            _journal(paths[0], recorded, state=upload.STATE_TRANSPORT, phase="registered",
                     upload_id="original-fixture-receipt", suppress_per_chunk_catbear=False)
            target = upload._sidecar_path("fixture-session", 0)
            before = target.read_bytes()
            session = _Session()
            with self.assertRaises(upload.UploadError):
                upload.pump_pending(session, max_retries=1)
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(session.calls, [])

    def test_known_later_receipt_blocks_session_before_uploading_first_chunk(self):
        with _fixture(2) as (_, paths, recorded):
            _journal(paths[0], recorded, expected_chunk_count=2,
                     suppress_per_chunk_catbear=True)
            later = device_profile.format_recorded_at(
                device_profile.recorded_at_to_wall_ms(recorded) / 1000 + 60)
            _journal(paths[1], later, index=1, expected_chunk_count=2,
                     upload_id="original-fixture-receipt", state=upload.STATE_DONE, phase="done",
                     suppress_per_chunk_catbear=True)
            files = [upload._sidecar_path("fixture-session", index) for index in range(2)]
            before = [path.read_bytes() for path in files]
            session = _Session()
            with self.assertRaises(upload.UploadError):
                _send(session, paths, recorded)
            self.assertEqual([path.read_bytes() for path in files], before)
            self.assertEqual(session.calls, [])

    def test_existing_register_order_governs_conflicting_live_option(self):
        for stored, live in ((False, True), (True, False), ("missing", True)):
            with self.subTest(stored=stored, live=live), _fixture() as (_, paths, recorded):
                row = _journal(paths[0], recorded, suppress_per_chunk_catbear=False)
                if stored == "missing":
                    del row["register_first"]
                else:
                    row["register_first"] = stored
                upload.save_sidecar(row)
                session = _Session()
                _send(session, paths, recorded, register_first=live)
                current = upload.load_sidecar("fixture-session", 0)
                expected_first = ("create" if stored is True else "sas")
                actual_first = ("create" if session.calls[0][1].startswith("/api/v1/uploads?")
                                else "sas")
                self.assertEqual(actual_first, expected_first)
                self.assertEqual(current.get("register_first", "missing"), stored)

    def test_invalid_persisted_register_order_is_rejected_before_probe(self):
        for invalid in (None, 0, 1, "false", [], {}):
            with self.subTest(invalid=invalid), _fixture() as (_, paths, recorded):
                _journal(paths[0], recorded, register_first=invalid,
                         suppress_per_chunk_catbear=False)
                target = upload._sidecar_path("fixture-session", 0)
                before = target.read_bytes()
                session = _Session()
                with mock.patch.object(upload, "_probe_duration_ms", return_value=60_000) as probe:
                    with self.assertRaises(upload.UploadError):
                        _send(session, paths, recorded)
                probe.assert_not_called()
                self.assertEqual(target.read_bytes(), before)
                self.assertEqual(session.calls, [])


class JournalOwnerBindingTests(unittest.TestCase):
    def test_known_owner_matches_authenticated_session_normalized(self):
        with _fixture() as (_, paths, recorded):
            original = _journal(paths[0], recorded, account_email=" Fixture@Example.Invalid ",
                                suppress_per_chunk_catbear=False)
            session = _Session()
            _send(session, paths, recorded)
            current = upload.load_sidecar("fixture-session", 0)
        self.assertEqual(session.created, 1)
        self.assertEqual(current["account_email"], original["account_email"])

    def test_other_or_unknown_authenticated_owner_blocks_before_any_effect(self):
        for owner in ("other-fixture@example.invalid", None, "", "   ", True, 7, [], {}):
            with self.subTest(owner=owner), _fixture() as (_, paths, recorded):
                _journal(paths[0], recorded, suppress_per_chunk_catbear=False)
                target = upload._sidecar_path("fixture-session", 0)
                before = target.read_bytes()
                session = _Session()
                session.email = owner
                with (mock.patch.object(upload, "_probe_duration_ms", return_value=60_000) as probe,
                      mock.patch.object(upload, "normalize_video", side_effect=lambda path: path) as normalize,
                      mock.patch.object(upload, "save_sidecar", wraps=upload.save_sidecar) as save):
                    with self.assertRaises(upload.UploadError) as caught:
                        _send(session, paths, recorded, normalize=True)
                probe.assert_not_called()
                normalize.assert_not_called()
                save.assert_not_called()
                self.assertEqual(target.read_bytes(), before)
                self.assertEqual(session.calls, [])
                self.assertNotIn("@", str(caught.exception))
                self.assertFalse(caught.exception.retryable)

    def test_divergent_later_owner_blocks_whole_session_before_effects(self):
        with _fixture(2) as (_, paths, recorded):
            later = device_profile.format_recorded_at(
                device_profile.recorded_at_to_wall_ms(recorded) / 1000 + 60)
            _journal(paths[0], recorded, expected_chunk_count=2,
                     suppress_per_chunk_catbear=True)
            _journal(paths[1], later, index=1, expected_chunk_count=2,
                     account_email="other-fixture@example.invalid", suppress_per_chunk_catbear=True)
            files = [upload._sidecar_path("fixture-session", index) for index in range(2)]
            before = [path.read_bytes() for path in files]
            session = _Session()
            with self.assertRaises(upload.UploadError):
                _send(session, paths, recorded)
            self.assertEqual([path.read_bytes() for path in files], before)
            self.assertEqual(session.calls, [])

    def test_legacy_unknown_owner_is_not_filled_from_profile_or_session(self):
        for owner in ("missing", None, ""):
            with self.subTest(owner=owner), _fixture() as (_, paths, recorded):
                row = _journal(paths[0], recorded, suppress_per_chunk_catbear=False,
                               account_email=None if owner == "missing" else owner)
                if owner == "missing":
                    del row["account_email"]
                else:
                    row["account_email"] = owner
                upload.save_sidecar(row)
                session = _Session()
                profile = SimpleNamespace(email=session.email,
                                          upload_device_meta=lambda: {"model": "fixture"},
                                          upload_platform_meta=lambda: {"os": "android"})
                _send(session, paths, recorded, profile=profile)
                current = upload.load_sidecar("fixture-session", 0)
                self.assertEqual(current.get("account_email", "missing"), owner)

    def test_new_journal_records_known_authenticated_owner(self):
        with _fixture() as (_, paths, recorded):
            session = _Session()
            session.email = " FIXTURE@EXAMPLE.INVALID "
            _send(session, paths, recorded)
            current = upload.load_sidecar("fixture-session", 0)
        self.assertEqual(current["account_email"], "fixture@example.invalid")

    def test_new_and_existing_journals_reject_profile_owner_divergence(self):
        for existing in (False, True):
            with self.subTest(existing=existing), _fixture() as (root, paths, recorded):
                if existing:
                    _journal(paths[0], recorded, suppress_per_chunk_catbear=False)
                    target = upload._sidecar_path("fixture-session", 0)
                    before = target.read_bytes()
                session = _Session()
                profile = SimpleNamespace(email="other-fixture@example.invalid",
                                          upload_device_meta=lambda: {"model": "fixture"},
                                          upload_platform_meta=lambda: {"os": "android"})
                with mock.patch.object(upload, "normalize_video", side_effect=lambda path: path) as normalize:
                    with self.assertRaises(upload.UploadError):
                        _send(session, paths, recorded, profile=profile, normalize=True)
                normalize.assert_not_called()
                self.assertEqual(session.calls, [])
                if existing:
                    self.assertEqual(target.read_bytes(), before)
                else:
                    self.assertFalse((root / "journals").exists())


class CompletionCliTests(unittest.TestCase):
    @staticmethod
    def _module():
        source = Path(__file__).resolve().parents[1] / "scripts" / "upload_video.py"
        specification = importlib.util.spec_from_file_location("completion_policy_cli", source)
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        return module

    def test_cli_default_and_compatible_alias_and_explicit_false(self):
        for options, expected in (([], True), (["--suppress-catbear"], True),
                                  (["--no-suppress-catbear"], False)):
            with self.subTest(options=options), _fixture() as (_, paths, _):
                module = self._module()
                result = UploadResult(session_id="fixture-session", org_key="fixture-org",
                                      task_id=None, chunks=[], total_size_bytes=1,
                                      total_duration_ms=60_000, recorded_at="fixture")
                arguments = ["upload_video.py", "fixture@example.invalid", str(paths[0]),
                             "fixture-org", "--no-normalize", *options]
                with (mock.patch.object(module.sys, "argv", arguments),
                      mock.patch.object(module, "_session", return_value=_Session()),
                      mock.patch.object(module.device_profile, "get_profile", return_value=None),
                      mock.patch.object(module.upload, "upload_session", return_value=result) as send,
                      redirect_stdout(io.StringIO())):
                    self.assertEqual(module.main(), 0)
                self.assertIs(send.call_args.kwargs["suppress_per_chunk_catbear"], expected)

    def test_cli_flag_options_are_mutually_exclusive(self):
        module = self._module()
        arguments = ["upload_video.py", "fixture@example.invalid", "fixture.mp4", "fixture-org",
                     "--suppress-catbear", "--no-suppress-catbear"]
        with (mock.patch.object(module.sys, "argv", arguments),
              mock.patch.object(module, "_session") as create_session,
              mock.patch.object(module.upload, "upload_session") as send):
            with self.assertRaises(SystemExit) as caught:
                module.main()
        self.assertEqual(caught.exception.code, 2)
        create_session.assert_not_called()
        send.assert_not_called()
