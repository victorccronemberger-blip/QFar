"""Offline contract tests for separating Minute signup from authentication."""
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from moneymin import config, minute_api


EMAIL = "new-account@example.invalid"
PASSWORD = "fixture-password-do-not-log"
PRIVATE_BODY = "fixture-private-provider-detail"


@pytest.fixture
def registration_setup(tmp_path, monkeypatch):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    monkeypatch.setattr(config, "SECRETS_DIR", secrets)
    monkeypatch.setattr(minute_api.token_store, "preflight_write", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("moneymin.account_bans.require_not_banned", lambda _email: None)
    monkeypatch.setattr("moneymin.org_policy.target_invite", lambda _email: "FIXTURE-CODE")
    profile = Mock()
    profile.device_id = "fixture-device"
    profile.headers.return_value = {"User-Agent": "fixture"}
    monkeypatch.setattr(minute_api.device_profile, "get_profile", lambda _email: profile)
    return secrets, profile


def test_register_identity_confirms_only_post_and_preserves_legacy_register(registration_setup):
    _secrets, profile = registration_setup
    response = minute_api.HttpResponse(201, '{"ignored":"remote payload"}', {"X-Private": "never surfaced"})
    with patch.object(minute_api, "_request_detailed", return_value=response) as post, \
         patch.object(minute_api, "login", side_effect=AssertionError("identity confirmation must not login")):
        assert minute_api.register_identity(EMAIL, PASSWORD, "fixture-code") is None

    args, kwargs = post.call_args
    assert args == (config.BASE_URL + "/api/v1/auth/web-register", "POST")
    assert kwargs["headers"] == {"User-Agent": "fixture"}
    assert kwargs["body"] == {"email": EMAIL, "password": PASSWORD,
                              "code": "FIXTURE-CODE", "device_id": "fixture-device"}
    profile.headers.assert_called_once_with(include_location=False)
    assert not list(Path(_secrets).glob("token_*.json"))

    order = []
    token = {"email": EMAIL, "idToken": "fixture-id"}
    with patch.object(minute_api, "register_identity", side_effect=lambda *a: order.append(("post", a))), \
         patch.object(minute_api, "login", side_effect=lambda *a: order.append(("login", a)) or token):
        assert minute_api.register(EMAIL, PASSWORD, "fixture-code") == token
    assert order == [("post", (EMAIL, PASSWORD, "fixture-code")),
                     ("login", (EMAIL, PASSWORD))]


def test_signup_429_is_sanitized_classified_and_not_admitted(registration_setup):
    _secrets, _profile = registration_setup
    response = minute_api.HttpResponse(429, PRIVATE_BODY, {"rEtRy-AfTeR": "37"})
    with patch.object(minute_api, "_request_detailed", return_value=response), \
         pytest.raises(minute_api.AuthError) as caught:
        minute_api.register_identity(EMAIL, PASSWORD)

    error = caught.value
    assert error.account_issue_code == "rate_limit"
    assert error.http_status == 429
    assert error.retry_after_seconds == 37
    assert error.remote_effect_possible is False
    assert PRIVATE_BODY not in str(error)


@pytest.mark.parametrize("body, expected_marker", [
    ('{"error":{"code":"EMAIL_EXISTS","message":"' + PRIVATE_BODY + '"}}', "EMAIL_EXISTS"),
    ('{"error":{"code":"email_already_exists"}}', "EMAIL_EXISTS"),
    ('Email already exists', "EMAIL_EXISTS"),
    ('"EMAIL_EXISTS"', "EMAIL_EXISTS"),
])
def test_only_whitelisted_duplicate_markers_survive_sanitization(
        registration_setup, body, expected_marker):
    _secrets, _profile = registration_setup
    response = minute_api.HttpResponse(400, body, {})
    with patch.object(minute_api, "_request_detailed", return_value=response), \
         pytest.raises(minute_api.AuthError) as caught:
        minute_api.register_identity(EMAIL, PASSWORD)

    assert caught.value.account_issue_code == "duplicate"
    assert caught.value.http_status == 400
    assert caught.value.remote_effect_possible is False
    assert expected_marker in str(caught.value)
    assert PRIVATE_BODY not in str(caught.value)


def test_other_signup_4xx_is_sanitized_and_explicitly_not_admitted(registration_setup):
    _secrets, _profile = registration_setup
    response = minute_api.HttpResponse(400, json.dumps({
        "error": {"code": "INVALID_ARGUMENT", "message": PRIVATE_BODY,
                  "email": EMAIL, "password": PASSWORD}}), {})
    with patch.object(minute_api, "_request_detailed", return_value=response), \
         pytest.raises(minute_api.AuthError) as caught:
        minute_api.register_identity(EMAIL, PASSWORD)

    assert caught.value.account_issue_code == "invalid_response"
    assert caught.value.http_status == 400
    assert caught.value.remote_effect_possible is False
    assert PRIVATE_BODY not in str(caught.value)
    assert EMAIL not in str(caught.value)
    assert PASSWORD not in str(caught.value)


def test_duplicate_word_inside_untrusted_detail_is_not_a_duplicate_marker(registration_setup):
    _secrets, _profile = registration_setup
    response = minute_api.HttpResponse(400, json.dumps({
        "message": f"{PRIVATE_BODY}: EMAIL_EXISTS for {EMAIL}"}), {})
    with patch.object(minute_api, "_request_detailed", return_value=response), \
         pytest.raises(minute_api.AuthError) as caught:
        minute_api.register_identity(EMAIL, PASSWORD)
    assert caught.value.account_issue_code == "invalid_response"
    assert caught.value.remote_effect_possible is False
    assert PRIVATE_BODY not in str(caught.value)
    assert EMAIL not in str(caught.value)


@pytest.mark.parametrize("status, expected_code, possible", [
    (408, "timeout", True),
    (500, "service", True),
    (-1, "network", True),
    (403, "forbidden", False),
])
def test_signup_uncertainty_marker_is_limited_to_ambiguous_responses(
        registration_setup, status, expected_code, possible):
    _secrets, _profile = registration_setup
    response = minute_api.HttpResponse(status, PRIVATE_BODY, {})
    with patch.object(minute_api, "_request_detailed", return_value=response), \
         pytest.raises(minute_api.AuthError if status != 403 else RuntimeError) as caught:
        minute_api.register_identity(EMAIL, PASSWORD)
    assert getattr(caught.value, "remote_effect_possible") is possible
    if status != 403:
        assert caught.value.account_issue_code == expected_code
        assert PRIVATE_BODY not in str(caught.value)


def test_login_429_uses_retry_after_and_never_echoes_firebase_body(registration_setup):
    secrets, _profile = registration_setup
    response = minute_api.HttpResponse(
        429, json.dumps({"error": {"message": PRIVATE_BODY}}), {"Retry-After": "19"})
    with patch("moneymin.account_bans.require_not_banned"), \
         patch.object(minute_api, "_request_detailed", return_value=response), \
         pytest.raises(minute_api.AuthError) as caught:
        minute_api.login(EMAIL, PASSWORD)

    assert caught.value.account_issue_code == "rate_limit"
    assert caught.value.http_status == 429
    assert caught.value.retry_after_seconds == 19
    assert PRIVATE_BODY not in str(caught.value)
    assert not list(Path(secrets).glob("token_*.json"))


@pytest.mark.parametrize("status, expected_code", [(401, "authentication"),
                                                     (403, "forbidden")])
def test_signup_auth_rejections_are_sanitized(status, expected_code, registration_setup):
    _secrets, _profile = registration_setup
    response = minute_api.HttpResponse(status, PRIVATE_BODY, {})
    with patch.object(minute_api, "_request_detailed", return_value=response), \
         pytest.raises(minute_api.AuthError) as caught:
        minute_api.register_identity(EMAIL, PASSWORD)
    assert caught.value.account_issue_code == expected_code
    assert caught.value.http_status == status
    assert caught.value.remote_effect_possible is False
    assert PRIVATE_BODY not in str(caught.value)


def test_retry_after_http_date_and_invalid_values_are_safe(monkeypatch):
    monkeypatch.setattr(minute_api.time, "time", lambda: 10.0)
    assert minute_api._retry_after_seconds({"Retry-After": "Thu, 01 Jan 1970 00:01:00 GMT"}) == 50
    assert minute_api._retry_after_seconds({"Retry-After": "not a date"}) is None
    assert minute_api._retry_after_seconds({}) is None
