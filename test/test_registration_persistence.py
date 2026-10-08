"""Registration checkpoints preserve ambiguous data before any remote effect."""
import copy
import json
import os
import threading
import traceback
from unittest.mock import Mock, patch

import pytest

from moneymin import config, credential_store, registration_proxy, secure_store
from moneymin.atomic_io import JsonStateError, save_json
from moneymin.web import registration_batch, registration_state, server


EMAIL = "qa@example.invalid"
SECOND_EMAIL = "second@example.invalid"
SECRET = "FICTIONAL-PERSISTENCE-SECRET"


@pytest.fixture
def stores(tmp_path, monkeypatch):
    assert "venom-offline-" in os.environ.get("QMONEY_USER_ROOT", "")
    data, secrets = tmp_path / "data", tmp_path / "secrets"
    data.mkdir()
    secrets.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "SECRETS_DIR", secrets)
    monkeypatch.setattr(server, "_BULK_REGISTER_STATE", {"state": "idle"})
    return data, secrets


def _result(email=EMAIL, *, created=True, steps=None, **changes):
    return {"email": email, "created": created,
            "error": None if created else "Cadastro incompleto.",
            "steps": {} if steps is None else steps, **changes}


def _batch(*results, state="done", total=None, **changes):
    return {"state": state, "total": len(results) if total is None else total,
            "completed": len(results), "created": sum(row["created"] for row in results),
            "failed": sum(not row["created"] for row in results),
            "results": list(results), "current_email": "", "current_step": "",
            "domain": "example.invalid", "stopped": False,
            "request_id": "fixture-request", "request_fingerprint": "a" * 64, **changes}


@pytest.mark.parametrize("candidate", [
    {"identity": {"nome": []}}, {"identity": []}, {"identity": {"birth_year": True}},
    {"identity": {"use_referral": 1}}, {"state": "unknown"}, {"state": []},
    {"error": {"password": SECRET}}, {"steps": []},
    {"steps": {"unknown": {"status": "ok"}}},
    {"steps": {"save_partial": {"status": "running"}}},
    {"steps": {"save_partial": {"status": []}}},
    {"steps": {"validate": {"status": "fail", "password": SECRET}}},
])
def test_invalid_registration_candidate_preserves_all_prior_accounts(stores, candidate):
    data, _ = stores
    registration_state.update(EMAIL, identity={"nome": "QA"}, state="incomplete")
    registration_state.update(SECOND_EMAIL, identity={"nome": "Second"}, state="complete")
    path = data / "account_registrations.json"
    before, expected = path.read_bytes(), registration_state.load()

    with pytest.raises(JsonStateError) as raised:
        registration_state.update(EMAIL, **candidate)

    assert path.read_bytes() == before
    assert registration_state.load() == expected
    assert SECRET not in "".join(traceback.format_exception(raised.value))


def test_checkpoint_round_trip_omits_credentials_and_preserves_missing_steps(stores):
    data, _ = stores
    identity = {"nome": "QA", "sobrenome": "Fixture", "email": EMAIL,
                "password": SECRET, "senha": SECRET, "birth_month": 4,
                "birth_year": 1990, "use_referral": False, "proxy_id": ""}
    registration_state.update(EMAIL, identity=identity, steps={
        "proxy": {"status": "skip", "detail": "Proxy desativado."},
        "save_partial": {"status": "ok"},
        "validate": {"status": "manual", "code": "check_required"},
    }, state="incomplete")
    row = registration_state.load()[EMAIL]
    assert row["identity"]["nome"] == "QA"
    assert "ban_check" not in row["steps"]
    assert SECRET.encode() not in (data / "account_registrations.json").read_bytes()
    assert registration_state.update(EMAIL, identity={})["identity"] == {}


def test_corrupt_checkpoint_is_not_replaced_by_new_account(stores):
    data, _ = stores
    path = data / "account_registrations.json"
    before = b'{"unknown":{"password":"FICTIONAL-PERSISTENCE-SECRET"}}'
    path.write_bytes(before)
    with pytest.raises(JsonStateError):
        registration_state.update(EMAIL, identity={"nome": "QA"})
    assert path.read_bytes() == before


@pytest.mark.parametrize("changes", [
    {"password": SECRET}, {"total": True}, {"completed": 2}, {"created": 0},
    {"failed": 1}, {"state": []}, {"state": "unknown"}, {"stopped": 1},
    {"current_step": []}, {"error": {"secret": SECRET}},
    {"request_fingerprint": "not-a-fingerprint"},
    {"request_fingerprint": ""}, {"request_id": "x" * 81},
    {"results": [{**_result(), "password": SECRET}]},
    {"results": [{**_result(), "created": 1}]},
    {"results": [{**_result(), "birth_year": True}]},
    {"results": [{**_result(), "steps": {"validate": {"status": []}}}]},
    {"results": [{**_result(), "steps": {"validate": {"status": "ok", "secret": SECRET}}}]},
])
def test_invalid_batch_candidate_cannot_replace_valid_progress(stores, changes):
    data, _ = stores
    path = data / "account_registration_batch.json"
    initial = _batch(_result())
    registration_batch.save(path, initial)
    before = path.read_bytes()
    with pytest.raises(JsonStateError):
        registration_batch.save(path, {**initial, **copy.deepcopy(changes)})
    assert path.read_bytes() == before
    assert registration_batch.load(path) == initial


def test_duplicate_result_identities_and_unstopped_incomplete_done_are_rejected(stores):
    data, _ = stores
    path = data / "account_registration_batch.json"
    for value in (_batch(_result(), _result()), _batch(_result(), total=2)):
        with pytest.raises(JsonStateError):
            registration_batch.save(path, value)
        assert not path.exists()


def test_partial_stopped_batch_accepts_all_real_result_and_step_fields(stores):
    data, _ = stores
    path = data / "account_registration_batch.json"
    steps = {key: {"status": status, "detail": "Fixture", "code": "fixture"}
             for key, status in zip(sorted(registration_state.STEPS),
                                    ["ok", "skip", "fail", "manual"] * 2)}
    value = _batch(
        _result(nome="QA", sobrenome="Fixture", gender=None, birth_month=None,
                birth_year=None, partial=False, removable=False, removed=False),
        _result(SECOND_EMAIL, created=False, steps=steps, nome="Second", sobrenome="Fixture",
                gender="male", birth_month=1, birth_year=1990, partial=True,
                removable=True, removed=True),
        total=3, stopped=True)
    registration_batch.save(path, value)
    before = path.read_bytes()
    server._restore_registration_batch()
    assert server._BULK_REGISTER_STATE == value
    assert registration_batch.load(path) == value
    assert path.read_bytes() == before


@pytest.mark.parametrize("state", ["running", "stopping"])
@pytest.mark.parametrize("has_result", [False, True])
def test_interruption_keeps_request_identity_without_assuming_remote_work_absent(stores, state, has_result):
    data, _ = stores
    path = data / "account_registration_batch.json"
    value = _batch(*([_result(created=False)] if has_result else []), state=state, total=2)
    registration_batch.save(path, value)
    before = path.read_bytes()
    server._restore_registration_batch()
    restored = server._BULK_REGISTER_STATE
    assert restored["state"] == "failed"
    assert restored["request_id"] == value["request_id"]
    assert restored["request_fingerprint"] == value["request_fingerprint"]
    assert restored["results"] == value["results"]
    assert path.read_bytes() == before


def test_invalid_restored_batch_is_not_published_or_automatically_reset(stores):
    data, _ = stores
    path = data / "account_registration_batch.json"
    value = _batch(_result(password=SECRET))
    save_json(path, value)
    before = path.read_bytes()
    for operation in (lambda: server._restore_registration_batch(),
                      lambda: registration_batch.save(path, {"state": "idle"})):
        with pytest.raises(JsonStateError) as raised:
            operation()
        assert SECRET not in "".join(traceback.format_exception(raised.value))
        assert path.read_bytes() == before
        assert server._BULK_REGISTER_STATE == {"state": "idle"}


@pytest.mark.parametrize("raw", [
    '{"schema":1,"email":"qa@example.invalid","password":"FIRST-FICTIONAL","password":"SECOND-FICTIONAL"}',
    '{"schema":1,"email":"other@example.invalid","email":"qa@example.invalid","password":"FICTIONAL"}',
    '{"schema":0,"schema":1,"email":"qa@example.invalid","password":"FICTIONAL"}',
    '{"schema":1,"email":"qa@example.invalid","password":"FIRST-FICTIONAL","passw\\u006frd":"SECOND-FICTIONAL"}',
    '{"schema":1,"email":"qa@example.invalid","password":"FICTIONAL","unused":NaN}',
])
def test_ambiguous_legacy_credential_never_migrates_or_overwrites_original(stores, monkeypatch, raw):
    _, secrets = stores
    monkeypatch.setattr(secure_store, "_crypt", lambda value, **_kwargs: bytes(byte ^ 0x5A for byte in value))
    path = credential_store.record_path(secrets, EMAIL)
    path.parent.mkdir(parents=True)
    path.write_bytes(raw.encode())
    before = path.read_bytes()
    for operation in (lambda: credential_store.lookup(secrets, EMAIL, strict=True),
                      lambda: credential_store.save(secrets, EMAIL, "replacement-fixture")):
        with pytest.raises(ValueError):
            operation()
        assert path.read_bytes() == before
    assert credential_store.load_all(secrets) == {}
    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path]


def test_unambiguous_bom_legacy_credential_still_migrates_and_round_trips(stores, monkeypatch):
    _, secrets = stores
    monkeypatch.setattr(secure_store, "_crypt", lambda value, **_kwargs: bytes(byte ^ 0x5A for byte in value))
    path = credential_store.record_path(secrets, EMAIL)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"schema": 1, "email": EMAIL,
                                                "password": SECRET}).encode())
    assert credential_store.lookup(secrets, EMAIL, strict=True) == SECRET
    assert SECRET.encode() not in path.read_bytes()
    assert credential_store.load_all(secrets) == {EMAIL: SECRET}
    assert list(path.parent.iterdir()) == [path]


@pytest.fixture
def registration_app(stores, monkeypatch):
    monkeypatch.setattr(server, "_BULK_REGISTER_LOCK", threading.RLock())
    monkeypatch.setattr(server, "_ACCOUNT_OPERATION_LOCK", threading.Lock())
    for name in ("RUNNER", "RECOVERY", "BALANCES_RUNNER", "ORG_MIGRATION"):
        monkeypatch.setattr(server, name, Mock(running=False))
    monkeypatch.setattr(server, "_hostinger_is_configured", lambda: True)
    monkeypatch.setattr(server, "_registration_domains", lambda: [{"domain": "example.invalid"}])
    monkeypatch.setattr(server, "_list_accounts", lambda: [])
    monkeypatch.setattr(registration_proxy, "validate_selection", lambda _selection: None)
    monkeypatch.setattr(server.identity, "gerar_identidade", lambda **_kwargs: {
        "email": EMAIL, "senha": SECRET, "nome": "QA", "sobrenome": "Fixture"})
    monkeypatch.setattr(server, "_full_register_account", Mock(return_value={
        "steps": {}, "error": None, "partial": False}))
    return server.create_app(for_testing=True)


def _start(client):
    return client.post("/api/accounts/bulk-register", json={
        "domain": "example.invalid", "count": 1, "request_id": "fixture-request"})


def test_corrupt_initial_checkpoint_fails_before_thread_and_preserves_bytes(registration_app):
    path = config.DATA_DIR / "account_registration_batch.json"
    before = b'{"state":"done","password":"FICTIONAL-PERSISTENCE-SECRET"}'
    path.write_bytes(before)
    with patch.object(server.threading, "Thread") as thread:
        response = _start(registration_app.test_client())
    assert response.status_code == 503
    thread.assert_not_called()
    server._full_register_account.assert_not_called()
    assert server._BULK_REGISTER_STATE["state"] == "failed"
    assert server._BULK_REGISTER_STATE["request_id"] == ""
    assert SECRET not in response.get_data(as_text=True)
    assert path.read_bytes() == before


def test_worker_final_checkpoint_rejection_does_not_escape_thread(registration_app):
    path = config.DATA_DIR / "account_registration_batch.json"
    with patch.object(server.threading, "Thread") as thread:
        assert _start(registration_app.test_client()).status_code == 200
    before = b'{"state":"running","password":"FICTIONAL-PERSISTENCE-SECRET"}'
    path.write_bytes(before)
    thread.call_args.kwargs["target"]()
    assert server._BULK_REGISTER_STATE["state"] == "failed"
    assert server._BULK_REGISTER_STATE["completed"] == 1
    assert server._BULK_REGISTER_STATE["created"] == 1
    assert server._BULK_REGISTER_STATE["request_id"] == "fixture-request"
    assert path.read_bytes() == before


def test_thread_start_and_terminal_checkpoint_failure_keep_ambiguous_disk_identity(registration_app):
    path = config.DATA_DIR / "account_registration_batch.json"
    real_save = server._save_registration_batch
    checkpoints = []

    def save_initial_only():
        if checkpoints:
            raise JsonStateError("Fixture invalid progress.")
        real_save()
        checkpoints.append(path.read_bytes())

    with patch.object(server.threading, "Thread") as thread, \
            patch.object(server, "_save_registration_batch", side_effect=save_initial_only):
        thread.return_value.start.side_effect = RuntimeError("Fixture cannot start worker.")
        response = _start(registration_app.test_client())
    assert response.status_code == 500
    server._full_register_account.assert_not_called()
    assert server._BULK_REGISTER_STATE["state"] == "failed"
    assert server._BULK_REGISTER_STATE["request_id"] == ""
    assert path.read_bytes() == checkpoints[0]
    server._restore_registration_batch()
    assert server._BULK_REGISTER_STATE["request_id"] == "fixture-request"
    assert server._BULK_REGISTER_STATE["state"] == "failed"
