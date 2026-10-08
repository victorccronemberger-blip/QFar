"""Admission and idempotency guards for account creation routes."""
import threading
import time
from unittest.mock import Mock, patch

import pytest

from moneymin import config, registration_proxy
from moneymin.web import server


@pytest.fixture
def registration_app(tmp_path, monkeypatch):
    data = tmp_path / "data"
    secrets = tmp_path / "secrets"
    data.mkdir()
    secrets.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "SECRETS_DIR", secrets)
    monkeypatch.setattr(config, "HOSTINGER_MAIL_PROFILES", [{
        "id": "fixture-mail", "name": "Fixture", "token": "fixture-only",
        "routes": ["example.invalid"],
    }])
    monkeypatch.setattr(server, "_BULK_REGISTER_STATE", {"state": "idle"})
    monkeypatch.setattr(server, "_BULK_REGISTER_LOCK", threading.RLock())
    monkeypatch.setattr(server, "_ACCOUNT_OPERATION_LOCK", threading.Lock())
    for name in ("RUNNER", "RECOVERY", "BALANCES_RUNNER", "ORG_MIGRATION"):
        monkeypatch.setattr(server, name, Mock(running=False))
    monkeypatch.setattr(server, "_hostinger_is_configured", lambda: True)
    monkeypatch.setattr(server, "_registration_domains", lambda: [
        {"domain": "example.invalid", "profile_id": "fixture-mail", "profile_name": "Fixture"}])
    monkeypatch.setattr(server, "_list_accounts", lambda: [])
    monkeypatch.setattr(registration_proxy, "validate_selection", lambda _selection: None)
    monkeypatch.setattr(server.identity, "gerar_identidade", lambda **_kwargs: {
        "email": "generated@example.invalid", "senha": "fixture-password",
        "nome": "Generated", "sobrenome": "Fixture"})
    monkeypatch.setattr(server, "_full_register_account", Mock(return_value={
        "steps": {}, "error": None, "partial": False}))
    return server.create_app(for_testing=True)


def _await_terminal(client):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        state = client.get("/api/accounts/bulk-register/status").get_json()
        if state.get("state") in {"done", "failed"}:
            return state
        time.sleep(0.005)
    pytest.fail("account registration worker did not terminate")


def _manual_body(**changes):
    return {"email": "qa@example.invalid", "password": "fixture-password", **changes}


@pytest.mark.parametrize("runner_name", ["RUNNER", "RECOVERY", "BALANCES_RUNNER"])
def test_synchronous_registration_obeys_worker_admission(registration_app, runner_name):
    client = registration_app.test_client()
    runner = getattr(server, runner_name)
    runner.running = True

    response = client.post("/api/accounts/register", json=_manual_body())

    assert response.status_code == 409, response.get_json()
    server._full_register_account.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"email": "not-an-email"},
    {"password": "p" * 4097},
    {"email": "qa@unconfigured.invalid"},
])
def test_synchronous_registration_rejects_invalid_credentials_or_unconfigured_domain(
        registration_app, changes):
    response = registration_app.test_client().post(
        "/api/accounts/register", json=_manual_body(**changes))

    assert response.status_code == 400, response.get_json()
    server._full_register_account.assert_not_called()


def test_synchronous_registration_rejects_invalid_proxy_before_registration(registration_app):
    with patch.object(registration_proxy, "validate_selection",
                      side_effect=ValueError("private proxy detail")):
        response = registration_app.test_client().post(
            "/api/accounts/register", json=_manual_body(proxy_id={"bad": "type"}))

    assert response.status_code == 400, response.get_json()
    assert "private proxy detail" not in response.get_data(as_text=True)
    server._full_register_account.assert_not_called()


def test_synchronous_storage_diagnostic_survives_an_unreadable_credential_path(registration_app):
    server._full_register_account.side_effect = OSError('FICTIONAL-PRIVATE-DISK-DETAIL')
    with patch.object(server.credential_store, 'record_path',
                      return_value=Mock(is_file=Mock(side_effect=OSError('FICTIONAL-PRIVATE-PATH')))):
        response = registration_app.test_client().post('/api/accounts/register', json=_manual_body())
    assert response.status_code == 503
    assert response.get_json()['code'] == 'local_registration_storage_failure'
    assert response.get_json()['ok'] is False
    assert 'retome o mesmo e-mail' in response.get_json()['error']
    assert 'FICTIONAL-PRIVATE' not in response.get_data(as_text=True)


@pytest.mark.parametrize("path", ["/api/accounts/register", "/api/accounts/register?async=1"])
@pytest.mark.parametrize("changes", [
    {"nome": []},
    {"sobrenome": {}},
    {"nome": "N" * 201},
    {"sobrenome": "S" * 201},
    {"sobrenome": "bad\nname"},
    {"nome": "bad\x7fname"},
])
def test_manual_registration_rejects_invalid_name_fields_before_work(
        registration_app, path, changes):
    response = registration_app.test_client().post(path, json=_manual_body(**changes))

    assert response.status_code == 400, (path, response.get_json())
    assert response.get_json()["code"] == "invalid_identity"
    server._full_register_account.assert_not_called()
    assert not (config.DATA_DIR / "account_registration_batch.json").exists()


@pytest.mark.parametrize("path", ["/api/accounts/register", "/api/accounts/register?async=1"])
def test_manual_registration_defaults_name_fields_from_email(path, registration_app):
    client = registration_app.test_client()
    response = client.post(path, json={"email": "QA@example.invalid",
                                      "password": "fixture-password"})
    assert response.status_code == 200, response.get_json()
    if "async" in path:
        assert _await_terminal(client)["created"] == 1
    identity_data = server._full_register_account.call_args.args[2]
    assert identity_data["nome"] == "qa"
    assert identity_data["sobrenome"] == ""


def test_name_validation_preserves_request_id_fingerprint(registration_app):
    client = registration_app.test_client()
    body = {**_manual_body(nome="QA", sobrenome="Tester"),
            "request_id": "identity-bound-request"}
    started = client.post("/api/accounts/register?async=1", json=body)
    assert started.status_code == 200, started.get_json()
    assert _await_terminal(client)["created"] == 1

    repeated = client.post("/api/accounts/register?async=1", json=body)
    assert repeated.status_code == 200 and repeated.get_json()["existing"] is True
    changed = client.post("/api/accounts/register?async=1", json={**body, "nome": "Changed"})
    assert changed.status_code == 409
    server._full_register_account.assert_called_once()


def test_non_object_json_creation_body_is_rejected_before_account_work(registration_app):
    client = registration_app.test_client()
    for path in ("/api/accounts/register", "/api/accounts/bulk-register"):
        response = client.post(path, json=["qa@example.invalid", "fixture-password"])
        assert response.status_code == 400, (path, response.get_json())
    server._full_register_account.assert_not_called()


def test_initial_checkpoint_failure_allows_same_request_id_retry(registration_app, monkeypatch):
    client = registration_app.test_client()
    body = {"count": 1, "domain": "example.invalid", "request_id": "retry-after-checkpoint"}
    save = server._save_registration_batch
    calls = 0

    def fail_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("fixture disk full")
        save()

    monkeypatch.setattr(server, "_save_registration_batch", fail_once)
    first = client.post("/api/accounts/bulk-register", json=body)
    assert first.status_code == 503, first.get_json()
    assert first.get_json()['not_admitted'] is True
    assert server._BULK_REGISTER_STATE["request_id"] == ""

    retry = client.post("/api/accounts/bulk-register", json=body)
    assert retry.status_code == 200, retry.get_json()
    assert retry.get_json().get("existing") is not True
    assert _await_terminal(client)["created"] == 1
    server._full_register_account.assert_called_once()


def test_thread_start_failure_allows_same_request_id_retry(registration_app, monkeypatch):
    client = registration_app.test_client()
    body = {"count": 1, "domain": "example.invalid", "request_id": "retry-after-thread-start"}

    class ThreadFactory:
        starts = 0

        def __init__(self, target, **_kwargs):
            self.target = target

        def start(self):
            type(self).starts += 1
            if type(self).starts == 1:
                raise RuntimeError("fixture thread unavailable")
            self.target()

    monkeypatch.setattr(server.threading, "Thread", ThreadFactory)
    first = client.post("/api/accounts/bulk-register", json=body)
    assert first.status_code == 500, first.get_json()
    assert first.get_json()['not_admitted'] is True
    assert server._BULK_REGISTER_STATE["request_id"] == ""

    retry = client.post("/api/accounts/bulk-register", json=body)
    assert retry.status_code == 200, retry.get_json()
    assert retry.get_json().get("existing") is not True
    assert client.get("/api/accounts/bulk-register/status").get_json()["created"] == 1
    server._full_register_account.assert_called_once()


def test_bulk_admission_rechecks_runner_after_local_account_snapshot(registration_app,
                                                                      monkeypatch):
    client = registration_app.test_client()

    def runner_starts_during_admission():
        server.RUNNER.running = True
        return []

    monkeypatch.setattr(server, "_list_accounts", runner_starts_during_admission)
    response = client.post("/api/accounts/bulk-register", json={
        "count": 1, "domain": "example.invalid", "request_id": "late-runner"})

    assert response.status_code == 409, response.get_json()
    assert server._BULK_REGISTER_STATE["state"] == "idle"
    server._full_register_account.assert_not_called()


def test_concurrent_http_admission_cannot_replace_running_batch(registration_app,
                                                                 monkeypatch):
    first_client = registration_app.test_client()
    second_client = registration_app.test_client()
    entered, release = threading.Event(), threading.Event()
    response_box = {}

    def paused_snapshot():
        entered.set()
        assert release.wait(3)
        return []

    monkeypatch.setattr(server, "_list_accounts", paused_snapshot)
    first = threading.Thread(target=lambda: response_box.setdefault(
        "first", first_client.post("/api/accounts/bulk-register", json={
            "count": 1, "domain": "example.invalid", "request_id": "first-admission"})))
    first.start()
    try:
        assert entered.wait(1)
        second = second_client.post("/api/accounts/bulk-register", json={
            "count": 1, "domain": "example.invalid", "request_id": "second-admission"})
        assert second.status_code == 409, second.get_json()
    finally:
        release.set()
        first.join(timeout=3)
    assert not first.is_alive()
    assert response_box["first"].status_code == 200
    assert _await_terminal(first_client)["created"] == 1
    server._full_register_account.assert_called_once()
