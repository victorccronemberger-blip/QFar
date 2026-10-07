"""Fictional accounts only: archive access bans, preserve ambiguous diagnoses."""
import json
from unittest.mock import Mock

import pytest

from moneymin import account_bans, config, credential_store, secure_store, token_store
from moneymin.atomic_io import save_json
from moneymin.web import account_health, registration_state, server

BAN = "banned@example.invalid"
KEEP = "active@example.invalid"
UNKNOWN = "unknown@example.invalid"
PAYOUT = "payout@example.invalid"
NOW = "2026-10-07T01:00:00+00:00"


def diagnosis(email, status="active", *, kind=None, confirmed=True):
    row = {"email": email, "status": status, "checked_at": NOW}
    if status == "disabled":
        row["issue"] = {"email": email, "code": "restricted", "restriction_confirmed": confirmed,
                        "reason": "Fictional confirmed restriction", "provider": "minute"}
    if kind:
        row["restriction_kind"] = kind
    return row


@pytest.fixture
def local(tmp_path, monkeypatch):
    data, secrets = tmp_path / "data", tmp_path / "secrets"
    data.mkdir()
    secrets.mkdir()
    for key, value in {"ROOT": tmp_path, "LIBRARY_ROOT": tmp_path, "DATA_DIR": data,
                       "MEDIA_DATA_DIR": data, "SECRETS_DIR": secrets}.items():
        monkeypatch.setattr(config, key, value)
    for key, value in {"PREFS_PATH": data / "webui_prefs.json", "BALANCES_PATH": data / "balances.json",
                       "CROWTADO_PW_PATH": secrets / "crowtado_passwords.json",
                       "ACCOUNT_HEALTH_PATH": data / "account_health.json"}.items():
        monkeypatch.setattr(server, key, value)
    monkeypatch.setattr(secure_store, "protect_json", lambda value: json.dumps(value).encode())
    monkeypatch.setattr(secure_store, "unprotect_json", lambda value: json.loads(value))
    assert not any(runner.running for runner in (server.RUNNER, server.RECOVERY, server.BALANCES_RUNNER))
    monkeypatch.setattr(server, "_BULK_REGISTER_STATE", {"state": "idle"})
    for email in (BAN, KEEP, UNKNOWN, PAYOUT):
        token_store.record_path(secrets, email).write_text(json.dumps({
            "email": email, "idToken": "fictional-token", "refreshToken": "fictional-refresh",
            "localId": email, "expires_at": 0}), encoding="utf8")
        credential_store.save(secrets, email, "fictional-password")
    save_json(server.ACCOUNT_HEALTH_PATH, {BAN: diagnosis(BAN, "disabled"),
        KEEP: diagnosis(KEEP), UNKNOWN: diagnosis(UNKNOWN, "inconclusive"),
        PAYOUT: diagnosis(PAYOUT, "disabled", kind="payout")})
    save_json(server.PREFS_PATH, {"selected_accounts": [BAN, KEEP, UNKNOWN, PAYOUT],
                                "org_keys": {BAN: "fictional", KEEP: "fictional"}})
    return data


def test_list_never_offers_confirmed_ban_even_before_archive(local):
    assert {row["email"] for row in server._list_accounts()} == {KEEP, UNKNOWN, PAYOUT}
    assert token_store.load(config.SECRETS_DIR, BAN) is not None


def test_account_api_archives_once_and_excludes_across_lists(local, monkeypatch):
    purge = Mock(wraps=account_bans.purge_local_records)
    monkeypatch.setattr(account_bans, "purge_local_records", purge)
    client = server.create_app(for_testing=True).test_client()
    for _ in range(2):
        response = client.get("/api/accounts")
        assert response.status_code == 200
        assert {row["email"] for row in response.json["accounts"]} == {KEEP, UNKNOWN, PAYOUT}
    assert purge.call_count == 1
    assert account_bans.banned_emails() == {BAN}
    assert BAN not in server._load_prefs()["selected_accounts"]
    assert BAN not in server._load_prefs()["org_keys"]
    assert token_store.load(config.SECRETS_DIR, BAN) is None
    assert credential_store.lookup(config.SECRETS_DIR, BAN) is None
    with pytest.raises(ValueError):
        account_bans.require_not_banned(BAN.upper())
    assert {row["email"] for row in server._list_accounts()} == {KEEP, UNKNOWN, PAYOUT}


def test_registration_only_ban_archived_without_token(local):
    token_store.delete(config.SECRETS_DIR, BAN)
    save_json(server.ACCOUNT_HEALTH_PATH, {})
    registration_state.update(BAN, state="incomplete", steps={
        "minute_register": {"status": "fail", "code": "restricted", "detail": "Fictional ban"}})
    client = server.create_app(for_testing=True).test_client()
    assert BAN not in {row["email"] for row in client.get("/api/accounts").json["accounts"]}
    assert account_bans.banned_emails() == {BAN}


@pytest.mark.parametrize("status, confirmed, kind, expected", [
    ("disabled", True, None, True), ("disabled", False, None, False),
    ("inconclusive", True, None, False), ("active", True, None, False),
    ("disabled", True, "payout", False)])
def test_only_current_confirmed_access_ban(status, confirmed, kind, expected):
    row = diagnosis(BAN, status, confirmed=confirmed, kind=kind)
    row["last_restriction"] = {"checked_at": NOW, "issue": diagnosis(BAN, "disabled")["issue"]}
    assert bool(account_health.confirmed_ban(BAN, row)) is expected


def test_newer_service_success_clears_registration_ban():
    registration = {"updated_at": "2026-10-06T01:00:00+00:00", "steps": {
        "minute_register": {"status": "fail", "code": "restricted"}}}
    assert account_health.confirmed_ban(BAN, diagnosis(BAN), registration) is None
    older = diagnosis(BAN)
    older["checked_at"] = "2026-10-05T01:00:00+00:00"
    assert account_health.confirmed_ban(BAN, older, registration)


def test_failed_archive_does_not_delete_access_or_offer_ban(local, monkeypatch):
    monkeypatch.setattr(server.banned_store, "save", Mock(side_effect=OSError("Fictional disk failure")))
    with pytest.raises(OSError):
        server._reconcile_account_bans()
    assert token_store.load(config.SECRETS_DIR, BAN) is not None
    assert credential_store.lookup(config.SECRETS_DIR, BAN) == "fictional-password"
    assert BAN not in {row["email"] for row in server._list_accounts()}


def test_removal_preserves_journals_campaigns_recovery_and_media(local):
    paths = [local / "campaign_fixture.json", local / "sidecar_migration.json",
             local / "sidecars" / "pending.json", config.ROOT / "recovery" / "pending.json"]
    for path in paths:
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"email": BAN, "video_path": "fictional-pending.mp4"}), encoding="utf8")
    media = local / "fictional-pending.mp4"
    media.write_bytes(b"fictional video")
    original = {path: path.read_bytes() for path in [*paths, media]}
    assert server._reconcile_account_bans() == [BAN]
    assert {path: path.read_bytes() for path in original} == original


def test_explicit_check_endpoint_archives_ban_after_both_diagnostics(local, monkeypatch):
    result = diagnosis(BAN, "disabled")
    monkeypatch.setattr(server, "_check_account_health", lambda email: result.copy())
    response = server.create_app(for_testing=True).test_client().post(f"/api/accounts/{BAN}/check")
    assert response.status_code == 400
    assert response.json["permanently_removed"] is True
    assert account_bans.banned_emails() == {BAN}


def test_batch_check_removes_only_new_access_ban(local, monkeypatch):
    save_json(server.ACCOUNT_HEALTH_PATH, {})
    results = {BAN: diagnosis(BAN, "disabled"), KEEP: diagnosis(KEEP),
               UNKNOWN: diagnosis(UNKNOWN, "inconclusive"), PAYOUT: diagnosis(PAYOUT, "disabled", kind="payout")}
    monkeypatch.setattr(server, "_check_account_health", lambda email: results[email].copy())
    client = server.create_app(for_testing=True).test_client()
    response = client.post("/api/accounts/check-all")
    assert response.status_code == 200 and response.json["total"] == 4
    assert {row["email"] for row in response.json["results"] if row.get("permanently_removed")} == {BAN}
    assert account_bans.banned_emails() == {BAN}
    assert {row["email"] for row in client.get("/api/accounts").json["accounts"]} == {KEEP, UNKNOWN, PAYOUT}


def test_same_service_success_does_not_clear_other_provider_ban():
    registration = {"updated_at": "2026-10-06T01:00:00+00:00", "steps": {
        "ban_check": {"status": "fail", "code": "restricted"}}}
    check = {"providers": {"minute": diagnosis(BAN), "crowtado": diagnosis(BAN, "inconclusive")}}
    assert account_health.confirmed_ban(BAN, check, registration)["provider"] == "crowtado"
