"""Preserve unconfirmed publications, using artificial stores and offline roots."""
from pathlib import Path
import json
import os
import subprocess
import sys

import pytest


@pytest.fixture
def local(tmp_path, monkeypatch):
    assert "venom-offline-" in os.environ.get("QMONEY_USER_ROOT", "")
    from moneymin import account_transfer as transfer, config, credential_store, token_store, tls
    data, secrets = tmp_path / "data", tmp_path / "secrets"
    data.mkdir()
    secrets.mkdir()
    for key, value in {"ROOT": tmp_path, "LIBRARY_ROOT": tmp_path, "DATA_DIR": data,
                       "MEDIA_DATA_DIR": tmp_path / "fake-work", "SECRETS_DIR": secrets}.items():
        monkeypatch.setattr(config, key, value)
    def forbidden(*args, **kwargs):
        raise AssertionError("Real authentication/provider/DPAPI operation forbidden")
    monkeypatch.setattr(transfer.minute_api, "login", forbidden)
    monkeypatch.setattr(credential_store, "save", forbidden)
    monkeypatch.setattr(tls, "urlopen", forbidden)
    monkeypatch.setattr(transfer.org_policy, "account_kind", lambda email: "claru")
    return tmp_path, transfer, credential_store, token_store, forbidden


def record(owner="account@example.invalid", password=None):
    return {"email": owner, "password": password,
            "token": {"email": owner, "localId": "fixture_uid", "idToken": "artificial_id",
                      "refreshToken": "artificial_refresh", "expires_at": 0}}


def paths(local, owner="account@example.invalid", existing=False):
    root, transfer, credential_store, token_store, _ = local
    token = token_store.record_path(transfer.config.SECRETS_DIR, owner)
    removed, passwords = root / "data/removed.json", root / "data/legacy_passwords.json"
    credential = credential_store.record_path(transfer.config.SECRETS_DIR, owner)
    credential.parent.mkdir(parents=True, exist_ok=True)
    if existing:
        old = record(owner)["token"]
        old["idToken"] = "artificial_old_id"
        token.write_text(json.dumps(old), encoding="utf-8")
        removed.write_bytes(json.dumps({"schema": 1, "emails": [owner]}).encode())
        credential.write_bytes(b"artificial_old_credential_bytes")
    return token, removed, passwords, credential


def snapshots(destinations):
    return {path: path.read_bytes() if path.exists() else None for path in destinations}


def assert_snapshots(before):
    for path, expected in before.items():
        assert (path.read_bytes() if path.exists() else None) == expected


COMPETING_WRITER = r'''
from pathlib import Path
import json, os, sys
root = Path(sys.argv[1]).resolve()
rows = json.loads(sys.argv[2])
for index, row in enumerate(rows):
    path = Path(row['path']).resolve()
    if not path.is_relative_to(root):
        raise RuntimeError('Artificial destination is outside its fixture root')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb' if index == 0 else 'wb') as stream:
        stream.write(row['payload'].encode('utf-8'))
        stream.flush()
        os.fsync(stream.fileno())
print('artificial_publications_completed')
'''


@pytest.mark.parametrize("conflict", ["uid", "token"])
@pytest.mark.parametrize("downstream", [False, True])
def test_valid_competing_publications_survive_unconfirmed_import(local, monkeypatch, conflict, downstream):
    root, transfer, _, token_store, _ = local
    owner = record()["email"]
    token, removed, passwords, credential = paths(local)
    removed.write_bytes(b'{"schema":1,"emails":[]}')
    credential.write_bytes(b"artificial_before_credential")
    competing = dict(record()["token"])
    competing["localId" if conflict == "uid" else "idToken"] = "artificial_competing_value"
    expected = {token: json.dumps(competing).encode()}
    if downstream:
        expected.update({removed: b'{"schema":1,"emails":["different@example.invalid"]}',
                         credential: b"artificial_competing_credential"})
    else:
        expected.update({removed: removed.read_bytes(), credential: credential.read_bytes()})
    real_link = token_store.os.link
    publications = []
    def competing_link(temporary, destination):
        assert Path(destination) == token
        payloads = [{"path": str(path), "payload": payload.decode()} for path, payload in expected.items()
                    if downstream or path == token]
        process = subprocess.run([sys.executable, "-c", COMPETING_WRITER, str(root), json.dumps(payloads)],
                                 capture_output=True, text=True, timeout=15, check=True)
        assert process.stdout.strip() == "artificial_publications_completed"
        publications.append("separate_process")
        return real_link(temporary, destination)
    restoration_writes = []
    def forbidden_restore(path, payload):
        restoration_writes.append(path)
        raise AssertionError("Unconfirmed publication does not authorize restoration")
    monkeypatch.setattr(token_store.os, "link", competing_link)
    monkeypatch.setattr(transfer, "save_bytes", forbidden_restore)
    with pytest.raises(token_store.TokenStoreError):
        transfer._save_record(record(password="artificial_password"), token, passwords, removed)
    assert publications == ["separate_process"]
    assert restoration_writes == []
    for path, payload in expected.items():
        assert path.read_bytes() == payload


@pytest.mark.parametrize("invalid", ["owner", "uid_type", "malformed"])
def test_invalid_competing_primary_and_untouched_destinations_are_preserved(local, monkeypatch, invalid):
    _, transfer, _, token_store, _ = local
    token, removed, passwords, credential = paths(local)
    removed.write_bytes(b'{"schema":1,"emails":[]}')
    credential.write_bytes(b"artificial_before_credential")
    document = record()["token"]
    if invalid == "owner":
        document["email"] = "different@example.invalid"
    elif invalid == "uid_type":
        document["localId"] = True
    payload = b"invalid artificial JSON" if invalid == "malformed" else json.dumps(document).encode()
    real_link = token_store.os.link
    def competing_link(temporary, destination):
        token.write_bytes(payload)
        return real_link(temporary, destination)
    monkeypatch.setattr(token_store.os, "link", competing_link)
    original_error = token_store.TokenStoreError
    before = snapshots((removed, credential))
    writes = []
    real_restore = transfer.save_bytes
    def restoration(path, payload):
        writes.append(path)
        return real_restore(path, payload)
    monkeypatch.setattr(transfer, "save_bytes", restoration)
    with pytest.raises(original_error):
        transfer._save_record(record(password="artificial_password"), token, passwords, removed)
    assert token.read_bytes() == payload
    assert_snapshots(before)
    assert writes == []


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("with_password", [False, True])
def test_failure_before_any_confirmed_publication_keeps_bytes_and_original_error(local, monkeypatch, existing, with_password):
    _, transfer, _, token_store, _ = local
    token, removed, passwords, credential = paths(local, existing=existing)
    before = snapshots((token, removed, credential))
    error = OSError("artificial_private_storage_detail")
    def failure(*args, **kwargs):
        raise error
    writes = []
    real_restore = transfer.save_bytes
    def restore(path, payload):
        writes.append(path)
        return real_restore(path, payload)
    monkeypatch.setattr(token_store, "save", failure)
    monkeypatch.setattr(transfer, "save_bytes", restore)
    with pytest.raises(OSError) as caught:
        transfer._save_record(record(password="artificial_password" if with_password else None), token, passwords, removed)
    assert caught.value is error
    assert_snapshots(before)
    assert writes == []


def test_publication_followed_by_unconfirmed_callee_failure_is_preserved(local, monkeypatch):
    _, transfer, _, token_store, _ = local
    token, removed, passwords, credential = paths(local)
    before = snapshots((removed, credential))
    real_publish = token_store._publish_exclusive
    error = OSError("artificial_post_publication_failure")
    def publish_then_fail(*args, **kwargs):
        real_publish(*args, **kwargs)
        raise error
    monkeypatch.setattr(token_store, "_publish_exclusive", publish_then_fail)
    with pytest.raises(OSError) as caught:
        transfer._save_record(record(), token, passwords, removed)
    assert caught.value is error
    assert token_store.read_file(token, record()["email"]) == record()["token"]
    assert_snapshots(before)


def test_password_only_unconfirmed_login_retains_bytes_private_error_and_batch_continuation(local, monkeypatch):
    _, transfer, _, token_store, _ = local
    owner = record()["email"]
    token, removed, passwords, credential = paths(local)
    removed.write_bytes(json.dumps({"schema": 1, "emails": [owner]}).encode())
    credential.write_bytes(b"artificial_before_credential")
    before = snapshots((removed, credential))
    published = b'{"email":"account@example.invalid"}'
    def fake_login(email, password):
        assert email == owner
        token.write_bytes(published)
        raise RuntimeError("artificial_private_login_detail")
    monkeypatch.setattr(transfer.minute_api, "login", fake_login)
    result = transfer.import_accounts(json.dumps([
        {"email": owner, "password": "artificial_password"}, record("next@example.invalid")]),
        apply=True, passwords_path=passwords, removed_path=removed)
    assert result["counts"]["error"] == 1
    assert result["counts"]["imported"] == 1
    assert token.read_bytes() == published
    # The subsequent successful row may clear only its own tombstone; here the
    # retained removed bytes are canonical already and the fake credential stays.
    assert credential.read_bytes() == before[credential]
    assert owner in json.loads(removed.read_text())["emails"]
    assert token_store.load(transfer.config.SECRETS_DIR, "next@example.invalid", migrate=False) is not None
    assert "artificial_private_login_detail" not in json.dumps(result)
    assert "artificial_password" not in json.dumps(result)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["before_removed", "before_credential", "after_credential"])
def test_confirmed_publication_rollback_contract_is_unchanged(local, monkeypatch, existing, failure):
    _, transfer, credential_store, _, _ = local
    token, removed, passwords, credential = paths(local, existing=existing)
    before = snapshots((token, removed, credential))
    original_error = OSError("artificial_private_save_detail")
    real_save_json = transfer.save_json
    def save_removed(path, value):
        if path == removed and failure == "before_removed":
            raise original_error
        real_save_json(path, value)
    def save_credential(directory, email, password):
        if failure == "after_credential":
            credential.parent.mkdir(parents=True, exist_ok=True)
            credential.write_bytes(b"artificial_own_credential_publication")
        raise original_error
    monkeypatch.setattr(transfer, "save_json", save_removed)
    monkeypatch.setattr(credential_store, "save", save_credential)
    with pytest.raises(OSError) as caught:
        transfer._save_record(record(password="artificial_password"), token, passwords, removed)
    assert caught.value is original_error
    assert_snapshots(before)


def test_confirmed_publication_failed_restoration_stops_batch_and_hides_details(local, monkeypatch):
    _, transfer, _, _, _ = local
    owner = record()["email"]
    token, removed, passwords, credential = paths(local, existing=True)
    old_removed, old_credential = removed.read_bytes(), credential.read_bytes()
    real_json, real_restore = transfer.save_json, transfer.save_bytes
    def save(path, value):
        if path == removed:
            raise OSError("artificial_private_save_detail")
        return real_json(path, value)
    def restore(path, payload):
        if path == token:
            raise OSError("artificial_private_restore_detail")
        return real_restore(path, payload)
    monkeypatch.setattr(transfer, "save_json", save)
    monkeypatch.setattr(transfer, "save_bytes", restore)
    result = transfer.import_accounts(json.dumps([record(password="artificial_password"), record("next@example.invalid")]),
                                     apply=True, passwords_path=passwords, removed_path=removed)
    assert result["counts"]["error"] == 2
    assert removed.read_bytes() == old_removed and credential.read_bytes() == old_credential
    assert not transfer.config.token_path("next@example.invalid").exists()
    assert "artificial_private_restore_detail" not in json.dumps(result)


def test_ordinary_preview_restore_and_duplicate_keep_existing_contract(local, monkeypatch):
    _, transfer, credential_store, token_store, _ = local
    owner = record()["email"]
    token, removed, passwords, credential = paths(local)
    removed.write_bytes(json.dumps({"schema": 1, "emails": [owner]}).encode())
    def save_credential(directory, email, password):
        path = credential_store.record_path(directory, email)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"artificial_protected_credential_bytes")
    monkeypatch.setattr(credential_store, "save", save_credential)
    document = json.dumps([record(password="artificial_password")])
    preview = transfer.import_accounts(document, apply=False, passwords_path=passwords, removed_path=removed)
    assert preview["counts"]["new"] == 1 and not token.exists() and not credential.exists()
    imported = transfer.import_accounts(document, apply=True, passwords_path=passwords, removed_path=removed)
    assert imported["counts"]["imported"] == 1
    assert token_store.read_file(token, owner) == record()["token"]
    assert json.loads(removed.read_text())["emails"] == []
    duplicate = transfer.import_accounts(document, apply=True, passwords_path=passwords, removed_path=removed)
    assert duplicate["counts"]["duplicate"] == 1
