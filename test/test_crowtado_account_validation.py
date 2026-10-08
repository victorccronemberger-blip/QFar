"""Clerk account flags and fresh login validation with isolated provider replies."""
from unittest.mock import patch
import pytest
from moneymin import crowtado


@pytest.mark.parametrize("flag,expected", [("banned", "restricted"), ("locked", "account_locked")])
@pytest.mark.parametrize("where", ["response", "client"])
def test_explicit_account_flags_are_classified_without_conflating_lockout(flag, expected, where):
    body = {"response":{"status":"complete", "created_session_id":"fixture-session"}}
    user = {"banned":False,"locked":False,flag:True}
    if where == "response":
        body["response"]["user"] = user
    else:
        body["client"] = {"sessions":[{"id":"fixture-session","user":user}]}
    with patch.object(crowtado.CrowtadoSession,"_fapi",side_effect=[(200,{}),(200,body)]):
        with pytest.raises(crowtado.CrowtadoError) as error:
            crowtado.login("fixture@example.invalid","fixture-password")
    assert error.value.account_issue_code == expected


def test_complete_login_with_clear_flags_remains_valid():
    body = {"response":{"status":"complete","created_session_id":"fixture-session","user":{"banned":False,"locked":False}}}
    with patch.object(crowtado.CrowtadoSession,"_fapi",side_effect=[(200,{}),(200,body)]):
        assert crowtado.login("fixture@example.invalid","fixture-password").session_id == "fixture-session"


def test_other_session_flags_do_not_mark_current_user_banned():
    crowtado._check_login_user_flags({"client":{"sessions":[{"id":"other-session","user":{"banned":True}}]}},"current-session")


def test_nonboolean_flags_are_inconclusive_rather_than_success():
    with pytest.raises(crowtado.CrowtadoError) as error:
        crowtado._check_login_user_flags({"response":{"user":{"banned":"false"}}},"fixture-session")
    assert error.value.account_issue_code == "invalid_response"


def test_locked_flag_carries_only_valid_lockout_duration():
    with pytest.raises(crowtado.CrowtadoError) as error:
        crowtado._check_login_user_flags(
            {"response": {"user": {"banned": False, "locked": True,
                                     "lockout_expires_in_seconds": 23}}}, "fixture-session")
    assert error.value.account_issue_code == "account_locked"
    assert error.value.retry_after_seconds == 23
    assert error.value.account_issue_code != "restricted"


def test_clerk_user_locked_error_is_temporary_and_parses_metadata():
    error = crowtado._remote_error("safe stage", 403, {
        "errors": [{"code": "user_locked", "long_message": "private provider text",
                    "meta": {"lockout_expires_in_seconds": 17}}]})
    assert error.account_issue_code == "account_locked"
    assert error.retry_after_seconds == 17
    assert error.provider_error_code == "user_locked"
    assert "private provider text" not in str(error)


def test_signup_response_records_rate_limit_without_provider_body():
    error = crowtado._signup_response_error(
        "https://clerk.crowtado.com/v1/client/sign_ups?secret=query", 429,
        {"errors": [{"code": "too_many_requests", "long_message": "private response"}]},
        {"Retry-After": "9"}, "signup_submission")
    assert error is not None
    assert error.account_issue_code == "rate_limit"
    assert error.retry_after_seconds == 9
    assert error.phase == "signup_submission"
    assert error.remote_effect_possible is True
    assert error.provider_error_code is None
    assert "private response" not in str(error)
    assert "secret=query" not in str(error)


def test_provider_error_code_is_whitelisted_and_does_not_retain_payload():
    error = crowtado._remote_error("safe", 401, {
        "errors": [{"code": "form_password_incorrect", "long_message": "sensitive text",
                    "meta": {"email": "secret@example.invalid", "token": "secret-token"}}]})
    assert error.provider_error_code == "form_password_incorrect"
    assert "sensitive text" not in str(error)
    assert "secret@example.invalid" not in str(error)
    assert "secret-token" not in str(error)

    unrecognized = crowtado._remote_error("safe", 400, {
        "errors": [{"code": "private_vendor_code", "long_message": "sensitive text"}]})
    assert unrecognized.provider_error_code is None
    assert "private_vendor_code" not in str(unrecognized)


def test_signup_response_ignores_non_signup_and_success_responses():
    assert crowtado._signup_response_error(
        "https://clerk.crowtado.com/v1/client/sign_ins", 429, {}, {}, "signup_submission") is None
    assert crowtado._signup_response_error(
        "https://clerk.crowtado.com/v1/client/sign_ups", 200, {}, {}, "signup_submission") is None


def test_signup_429_keeps_cooldown_when_response_body_is_not_json():
    class Response:
        url = "https://clerk.crowtado.com/v1/client/sign_ups"
        status = 429
        headers = {"retry-after": "12"}

        @staticmethod
        def json():
            raise ValueError("body must not escape")

    error = crowtado._signup_response_error_from_response(Response(), "signup_submission")
    assert error is not None
    assert error.account_issue_code == "rate_limit"
    assert error.retry_after_seconds == 12
    assert "body must not escape" not in str(error)


def test_account_lock_does_not_trigger_browser_fallback():
    error = crowtado.CrowtadoError("A Crowtado bloqueou o acesso desta conta", code="account_locked")
    assert not crowtado.can_use_browser_fallback(error)
