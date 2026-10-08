import json
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from moneymin import minute_api as api, upload


class AuthContractTests(unittest.TestCase):
    def test_invalid_login_never_persists_credentials(self):
        cases = [[], {}, {"idToken": 123, "refreshToken": "r", "expiresIn": "3600"},
                 {"idToken": "t", "refreshToken": "r", "expiresIn": -1},
                 {"idToken": "t", "refreshToken": "r", "expiresIn": True}]
        for payload in cases:
            with self.subTest(payload=payload), \
                 mock.patch("moneymin.account_bans.require_not_banned"), \
                 mock.patch.object(api, "_request_detailed", return_value=api.HttpResponse(
                     200, json.dumps(payload), {})), \
                 mock.patch.object(api, "save_json") as save:
                with self.assertRaises(api.AuthError) as error:
                    api.login("test@example.com", "secret")
                self.assertEqual(error.exception.account_issue_code, "invalid_response")
                save.assert_not_called()

    def test_invalid_refresh_preserves_old_credentials(self):
        original = {"idToken": "old", "refreshToken": "old-refresh", "expires_at": 1}
        for value in (None, [], 123, " "):
            token = original.copy()
            with self.subTest(value=value), mock.patch.object(api, "_request", return_value=(
                    200, json.dumps({"id_token": value, "refresh_token": "new", "expires_in": "3600"}))):
                with self.assertRaises(api.AuthError):
                    api._refresh(token)
            self.assertEqual(token, original)

    def test_valid_refresh_updates_credentials_atomically(self):
        token = {"refreshToken": "old", "email": "test@example.com"}
        with mock.patch.object(api, "_request", return_value=(200, json.dumps({
                "id_token": "new", "refresh_token": "new-refresh", "expires_in": "3600"}))):
            updated = api._refresh(token)
            self.assertIsNot(updated, token)
        self.assertEqual(token, {"refreshToken": "old", "email": "test@example.com"})
        token = updated
        self.assertEqual(token["idToken"], "new")
        self.assertEqual(token["email"], "test@example.com")
        self.assertGreater(token["expires_at"], time.time())

    def test_version_gate_is_observed_after_each_auth_retry(self):
        for detailed in (False, True):
            for statuses in ([401, 403], [401, 401, 403]):
                session = api.Session({'email': 'fixture@example.invalid', "idToken": "old", "expires_at": time.time() + 3600})
                session._live = True
                responses = [api.HttpResponse(code, "gate", {}) if detailed else (code, "gate")
                             for code in statuses]
                with self.subTest(detailed=detailed, statuses=statuses), \
                     mock.patch.object(session, "_check_write_policy"), \
                     mock.patch.object(session, "refresh"), \
                     mock.patch.object(session, "_relogin"), \
                     mock.patch.object(api, "_maybe_latch_version_gate") as gate, \
                     mock.patch.object(api, "_request_detailed" if detailed else "_request",
                                       side_effect=responses) as request:
                    result = (session.request_detailed if detailed else session.request)("GET", "/test")
                    expected_status = statuses[min(2, len(statuses)) - 1]
                    self.assertEqual(result.status if detailed else result[0], expected_status)
                    if expected_status == 403:
                        gate.assert_called_once_with("gate")
                    else:
                        gate.assert_not_called()  # The unreachable third response is not observed.
                    self.assertEqual(request.call_count, min(2, len(statuses)))
                    self.assertFalse(session._refreshing)


class RecoveryContractTests(unittest.TestCase):
    def test_conflict_word_is_not_proof_of_completion(self):
        with mock.patch.object(upload, "_session_request", return_value=(409, '{"expected":"completed"}', {})), \
             mock.patch.object(upload, "get_upload", return_value={"status": "pending"}):
            with self.assertRaises(upload.UploadError):
                upload.complete_upload(object(), "upload", 10)

    def test_conflict_requires_confirmed_remote_state(self):
        with mock.patch.object(upload, "_session_request", return_value=(409, '{}', {})), \
             mock.patch.object(upload, "get_upload", return_value={"status": "completed"}) as lookup:
            self.assertEqual(upload.complete_upload(object(), "upload", 10)["status"], "completed")
            lookup.assert_called_once()

    def test_malformed_success_is_not_completion(self):
        for body in ("[]", "null", "invalid"):
            with self.subTest(body=body), mock.patch.object(upload, "_session_request", return_value=(200, body, {})):
                with self.assertRaises(upload.UploadError):
                    upload.complete_upload(object(), "upload", 10)

    def journal(self, **changes):
        result = dict(account_email="a@example.com", org_key="org", session_id="session",
                      chunk_index=0, expected_chunk_count=1, upload_id="upload",
                      state=upload.STATE_COMPLETING, phase="awaiting_finalize", finalize_requested=True)
        result.update(changes)
        return result

    def pump(self, journals):
        with mock.patch.object(upload, "list_sidecars", return_value=journals), \
             mock.patch.object(upload, "evaluate_upload", return_value={"checks": [{"status": "pass"}]}), \
             mock.patch.object(upload, "save_sidecar"), \
             mock.patch.object(upload, "load_sidecar", return_value=None), \
             mock.patch.object(upload, "_remove_sidecar_archive"), \
             mock.patch.object(upload, "upload_session") as send, \
             mock.patch.object(upload, "complete_upload") as complete, \
             mock.patch.object(upload, "_finalize_session", return_value=(True, 200)) as finalize:
            result = upload.pump_pending(SimpleNamespace(email="a@example.com"))
            send.assert_not_called()
            complete.assert_not_called()
            return result, finalize.call_count

    def test_finalize_resume_does_not_require_or_resend_local_video(self):
        for phase in ("awaiting_finalize", "finalize"):
            with self.subTest(phase=phase):
                result, calls = self.pump([self.journal(phase=phase)])
                self.assertEqual(calls, 1)
                self.assertTrue(result[0]["finalized"])
                self.assertEqual(result[0]["state"], upload.STATE_DONE)

    def test_finalize_recovery_preserves_campaign_identity(self):
        context = {"registry_key": "category", "clip_uid": "clip", "task_id": "task"}
        result, calls = self.pump([self.journal(campaign_context=context)])
        self.assertEqual(calls, 1)
        self.assertEqual(result[0]["campaign_context"], context)

    def test_crash_after_chunk_complete_resumes_only_session_finalization(self):
        for finalized in (False, None):
            with self.subTest(finalized=finalized):
                journal = self.journal(state=upload.STATE_DONE, phase="done")
                if finalized is not None:
                    journal["finalized"] = finalized
                result, calls = self.pump([journal])
                self.assertEqual(calls, 1)
                self.assertTrue(result[0]["finalized"])

    def test_already_finalized_chunks_are_not_reprocessed(self):
        result, calls = self.pump([self.journal(
            state=upload.STATE_DONE, phase="done", finalized=True)])
        self.assertEqual(result, [])
        self.assertEqual(calls, 0)

    def test_malformed_resume_fields_do_not_mutate_or_call_services(self):
        for changes in ({"chunk_index": True}, {"chunk_index": "0"},
                        {"crash_resumes": "0"}, {"attempts": "0"},
                        {"finalize_requested": "true"}):
            with self.subTest(changes=changes), \
                 mock.patch.object(upload, "list_sidecars", return_value=[self.journal(**changes)]), \
                 mock.patch.object(upload, "save_sidecar") as save, \
                 mock.patch.object(upload, "_finalize_session") as finalize:
                with self.assertRaises(upload.UploadError):
                    upload.pump_pending(SimpleNamespace(email="a@example.com"))
                save.assert_not_called()
                finalize.assert_not_called()

    def test_invalid_count_before_transport_cannot_be_normalized_or_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "local.mp4"
            video.write_bytes(b"local-video")
            for count in ("1", "invalid", 1.0, True, False, 0, -1, None, [], {}):
                journal = self.journal(state=upload.STATE_CREATING, phase="queued",
                                       local_video_path=str(video), expected_chunk_count=count)
                with self.subTest(count=count), \
                     mock.patch.object(upload, "list_sidecars", return_value=[journal]), \
                     mock.patch.object(upload, "save_sidecar") as save, \
                     mock.patch.object(upload, "upload_session") as send, \
                     mock.patch.object(upload, "complete_upload") as complete, \
                     mock.patch.object(upload, "_finalize_session") as finalize:
                    with self.assertRaises(upload.UploadError):
                        upload.pump_pending(SimpleNamespace(email="a@example.com"))
                    save.assert_not_called()
                    send.assert_not_called()
                    complete.assert_not_called()
                    finalize.assert_not_called()
                self.assertEqual(journal["expected_chunk_count"], count)

    def test_missing_legacy_counts_and_attempts_still_allow_finalize(self):
        journal = self.journal()
        journal.pop("expected_chunk_count")
        result, calls = self.pump([journal])
        self.assertEqual(calls, 1)
        self.assertTrue(result[0]["finalized"])

    def test_duplicate_selected_chunks_cannot_mutate_or_repeat_services(self):
        for phase in ("queued", "transport_done", "awaiting_finalize"):
            journals = [self.journal(state=upload.STATE_CREATING, phase=phase),
                        self.journal(state=upload.STATE_CREATING, phase=phase)]
            originals = [row.copy() for row in journals]
            with self.subTest(phase=phase), \
                 mock.patch.object(upload, "list_sidecars", return_value=journals), \
                 mock.patch.object(upload, "save_sidecar") as save, \
                 mock.patch.object(upload, "upload_session") as send, \
                 mock.patch.object(upload, "complete_upload") as complete, \
                 mock.patch.object(upload, "_finalize_session") as finalize:
                with self.assertRaises(upload.UploadError):
                    upload.pump_pending(SimpleNamespace(email="a@example.com"))
                self.assertEqual(journals, originals)
                save.assert_not_called()
                send.assert_not_called()
                complete.assert_not_called()
                finalize.assert_not_called()

    def test_duplicate_other_account_journals_do_not_block_selected_owner(self):
        journals = [self.journal(), self.journal(account_email="b@example.com"),
                    self.journal(account_email="b@example.com")]
        result, calls = self.pump(journals)
        self.assertEqual(len(result), 1)
        self.assertEqual(calls, 0)  # mixed ownership still prevents finalization
        self.assertEqual(journals[0]["crash_resumes"], 1)
        self.assertNotIn("crash_resumes", journals[1])
        self.assertNotIn("crash_resumes", journals[2])

    def test_mixed_owners_or_orgs_cannot_finalize_together(self):
        for changes in ({"account_email": "b@example.com"}, {"org_key": "other"}):
            with self.subTest(changes=changes):
                journals = [self.journal(expected_chunk_count=2),
                            self.journal(chunk_index=1, expected_chunk_count=2, **changes)]
                self.assertEqual(self.pump(journals)[1], 0)

    def test_mixed_tasks_or_clip_contexts_cannot_finalize_together(self):
        for changes in ({"task_id": "another-task"},
                        {"campaign_context": {"registry_key": "task", "clip_uid": "other"}}):
            with self.subTest(changes=changes):
                journals = [self.journal(expected_chunk_count=2),
                            self.journal(chunk_index=1, expected_chunk_count=2, **changes)]
                self.assertEqual(self.pump(journals)[1], 0)

    def test_missing_duplicate_or_failed_chunks_prevent_finalize(self):
        cases = [[self.journal(expected_chunk_count=2)],
                 [self.journal(expected_chunk_count=2), self.journal(chunk_index=1, expected_chunk_count=3)],
                 [self.journal(expected_chunk_count=2), self.journal(
                     chunk_index=1, expected_chunk_count=2, state=upload.STATE_FAILED, phase="finalize")]]
        for journals in cases:
            with self.subTest(journals=journals):
                self.assertEqual(self.pump(journals)[1], 0)

    def test_wrong_authenticated_account_is_rejected(self):
        with mock.patch.object(upload, "list_sidecars", return_value=[]), \
             self.assertRaises(upload.UploadError):
            upload.pump_pending(SimpleNamespace(email="a@example.com"), account_email="b@example.com")
