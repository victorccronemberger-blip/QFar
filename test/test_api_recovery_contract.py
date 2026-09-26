import json
import time
import unittest
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
                 mock.patch.object(api, "_request", return_value=(200, json.dumps(payload))), \
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
            self.assertIs(api._refresh(token), token)
        self.assertEqual(token["idToken"], "new")
        self.assertEqual(token["email"], "test@example.com")
        self.assertGreater(token["expires_at"], time.time())

    def test_version_gate_is_observed_after_each_auth_retry(self):
        for detailed in (False, True):
            for statuses in ([401, 403], [401, 401, 403]):
                session = api.Session({"idToken": "old", "expires_at": time.time() + 3600})
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
                    self.assertEqual(result.status if detailed else result[0], 403)
                    gate.assert_called_once_with("gate")
                    self.assertEqual(request.call_count, len(statuses))
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

    def test_mixed_owners_or_orgs_cannot_finalize_together(self):
        for changes in ({"account_email": "b@example.com"}, {"org_key": "other"}):
            with self.subTest(changes=changes):
                journals = [self.journal(expected_chunk_count=2),
                            self.journal(chunk_index=1, expected_chunk_count=2, **changes)]
                self.assertEqual(self.pump(journals)[1], 0)

    def test_missing_duplicate_or_failed_chunks_prevent_finalize(self):
        cases = [[self.journal(expected_chunk_count=2)],
                 [self.journal(expected_chunk_count="invalid")],
                 [self.journal(expected_chunk_count=2), self.journal(chunk_index=1, expected_chunk_count=3)],
                 [self.journal(expected_chunk_count=2), self.journal(expected_chunk_count=2)],
                 [self.journal(expected_chunk_count=2), self.journal(
                     chunk_index=1, expected_chunk_count=2, state=upload.STATE_FAILED, phase="finalize")]]
        for journals in cases:
            with self.subTest(journals=journals):
                self.assertEqual(self.pump(journals)[1], 0)

    def test_wrong_authenticated_account_is_rejected(self):
        with mock.patch.object(upload, "list_sidecars", return_value=[]), \
             self.assertRaises(upload.UploadError):
            upload.pump_pending(SimpleNamespace(email="a@example.com"), account_email="b@example.com")
