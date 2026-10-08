"""Confirmed bans remove access without rewriting delivery/admission evidence."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path

import pytest

from moneymin import (account_bans, campaign_evidence, campaign_start_store, config,
                      credential_store, media_lifecycle, original_capture, recovery,
                      secure_store, sent_registry, token_store, upload)
from moneymin.atomic_io import save_json
from moneymin.recovery_errors import RecoveryReadError
from moneymin.web import server


BAN = "banned@example.invalid"
OTHERS = ("second@example.invalid", "third@example.invalid")
ORG, TASK, CLIP, KEY = "fixture-org", "fixture-task", "fixture-clip", "minute|fixture-task"


@pytest.fixture
def local(tmp_path, monkeypatch):
    assert "venom-offline-" in os.environ.get("QMONEY_USER_ROOT", "")
    root, library = tmp_path / "installation", tmp_path / "library"
    data, secrets, media = root / "data", root / "secrets", library / "data" / "ego4d"
    for path in (data, secrets, media, data / "sidecars"):
        path.mkdir(parents=True, exist_ok=True)
    for name, value in {"ROOT": root, "LIBRARY_ROOT": library, "DATA_DIR": data,
                        "SECRETS_DIR": secrets, "MEDIA_DATA_DIR": library / "data"}.items():
        monkeypatch.setattr(config, name, value)
    for name, value in {"PREFS_PATH": data / "webui_prefs.json", "BALANCES_PATH": data / "balances.json",
                        "CROWTADO_PW_PATH": secrets / "crowtado_passwords.json",
                        "ACCOUNT_HEALTH_PATH": data / "account_health.json"}.items():
        monkeypatch.setattr(server, name, value)
    monkeypatch.setattr(server, "_BULK_REGISTER_STATE", {"state": "idle"})
    monkeypatch.setattr(secure_store, "protect_json", lambda value: json.dumps(value).encode())
    monkeypatch.setattr(secure_store, "unprotect_json", lambda value: json.loads(value))
    for email in (BAN, *OTHERS):
        token_store.record_path(secrets, email).write_text(json.dumps({
            "email": email, "idToken": "fictional-token", "refreshToken": "fictional-refresh",
            "localId": email, "expires_at": 0}), encoding="utf8")
        credential_store.save(secrets, email, "fictional-password")
    save_json(server.PREFS_PATH, {"selected_accounts": [BAN, *OTHERS], "org_keys": {BAN: ORG}})
    save_json(server.ACCOUNT_HEALTH_PATH, {BAN: {"status": "disabled"}, OTHERS[0]: {"status": "active"}})
    return root, library, media


def owned(media, name="owned.mp4"):
    path = media / name
    path.write_bytes(f"Inert fictional provider media {name}".encode())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    media_lifecycle.record_managed_media(path, root=media, provider="ego4d",
        role="prepared_video", expected_digest=digest)
    return path, digest


def journal(video, digest, sid, email=BAN, *, confirmed=False, history=None):
    archive = config.DATA_DIR / "sidecars" / f"{sid}.data.zip"
    archive.write_bytes(f"Inert fictional archive {sid}".encode())
    row = {"session_id": sid, "chunk_index": 0, "expected_chunk_count": 1,
        "account_email": email, "org_key": ORG, "task_id": TASK,
        "upload_id": f"upload-{sid}", "log_id": f"{sid}_0", "filename": f"{sid}_0.mp4",
        "recorded_at": "2026-10-08T04:10:30.988Z", "duration_ms": 341866,
        "state": "done" if confirmed else "failed", "phase": "done" if confirmed else "complete",
        "finalized": confirmed, "campaign_reconciled": confirmed, "evaluation_required": False,
        "register_first": True, "native_response_schema": True, "create_attempted": True,
        "finalize_requested": True, "size_bytes": video.stat().st_size,
        "local_video_path": str(video), "video_content_sha256": digest,
        "sidecar_data_path": str(archive), "sidecar_size_bytes": archive.stat().st_size,
        "sidecar_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "campaign_context": {"registry_key": KEY, "clip_uid": CLIP, "task_id": TASK}}
    if history:
        row["campaign_context"]["history_name"] = history
    upload.save_sidecar(row)
    return row


def ban():
    server._ban_accounts([{"email": BAN, "restriction_confirmed": True,
        "reason": "Fictional platform-confirmed restriction", "stage": "Fictional auth"}])
    assert BAN in account_bans.banned_emails()
    assert token_store.load(config.SECRETS_DIR, BAN) is None
    assert credential_store.lookup(config.SECRETS_DIR, BAN) is None
    assert token_store.load(config.SECRETS_DIR, OTHERS[0]) is not None


def test_ban_preserves_three_confirmed_deliveries_publication_and_sent_index(local):
    _root, _library, media = local
    video, digest = owned(media)
    name = "campaign_three_deliveries.json"
    rows = [journal(video, digest, f"confirmed-{index}", email, confirmed=True, history=name)
            for index, email in enumerate((BAN, *OTHERS))]
    history = {"status": "done", "items": [{"clip_uid": CLIP, "task_id": TASK,
        "registry_key": KEY, "accounts": [{"email": row["account_email"], "org_key": ORG,
            "session_id": row["session_id"], "ok": True, "finalized": True} for row in rows]}]}
    save_json(config.DATA_DIR / name, history)
    sent_registry.mark_sent_many([(KEY, CLIP, email) for email in (BAN, *OTHERS)])
    reset = {"all": [], "scenarios": {}, "completed_sessions": [], "diagnostic_owner": BAN}
    save_json(config.DATA_DIR / "sent_reset_history.json", reset)
    paths = [config.DATA_DIR / name, config.DATA_DIR / "sent_videos.json",
             config.DATA_DIR / "sent_reset_history.json", video, video.with_name(video.name + ".managed.json")]
    paths.extend(path for path in (config.DATA_DIR / "sidecars").iterdir() if not path.name.endswith(".lock"))
    before = {path: path.read_bytes() for path in paths}
    assert all(campaign_evidence.publication_registered([row], campaign_evidence.publication_index()) for row in rows)
    ban()
    assert {path: path.read_bytes() for path in paths} == before
    assert sent_registry.sent_emails(KEY, CLIP) == {BAN, *OTHERS}
    assert sent_registry._reset_history() == reset
    assert all(campaign_evidence.publication_registered([row], campaign_evidence.publication_index()) for row in rows)
    assert BAN not in server._load_prefs()["selected_accounts"]
    assert BAN not in server._load_account_health_history()


def test_ban_keeps_individual_claim_and_original_capture_reservation_valid(local):
    _root, _library, media = local
    claimed, claim_hash = owned(media, "claimed.mp4")
    reserved, reserved_hash = owned(media, "original-reserved.mp4")
    start_id, original_id = "individual-start", "original-reservation"
    campaign_start_store.claim(start_id, kind="original", receipt_id="fixture-preflight",
        body={"account_email": BAN, "task_id": TASK},
        bindings={"accounts": [{"email": BAN, "org_key": ORG}], "tasks": [TASK]},
        protected_assets=[{"path": str(claimed), "sha256": claim_hash}])
    expected_claim = campaign_start_store.lookup(start_id)
    reservation = {"version": 1, "bindings": {original_id: {
        "account_email": BAN, "org_key": ORG, "task_id": TASK, "session_id": original_id,
        "phase": "reserved", "digest": "a" * 64, "content_digest": "b" * 64,
        "media_sha256": [reserved_hash], "sidecar_sha256": ["c" * 64]}}}
    save_json(config.DATA_DIR / "original_capture_reservations.json", reservation)
    assert original_capture._read_reservations() == reservation
    paths = [config.DATA_DIR / "start_requests.json", config.DATA_DIR / "original_capture_reservations.json"]
    before = {path: path.read_bytes() for path in paths}
    ban()
    assert {path: path.read_bytes() for path in paths} == before
    assert campaign_start_store.lookup(start_id) == expected_claim
    assert campaign_start_store.public_status(start_id)["found"] is True
    assert original_capture._read_reservations() == reservation
    protection = recovery.media_cleanup_protection()
    assert claimed in protection["paths"] and reserved_hash in protection["sha256"]
    result = media_lifecycle.cleanup_managed_media([claimed, reserved], allowed_roots=(media,))
    assert result["files"] == 0 and not result["errors"]
    assert claimed.is_file() and reserved.is_file()


def test_ban_preserves_pending_journal_zip_and_media_guard(local):
    root, library, media = local
    video, digest = owned(media)
    row = journal(video, digest, "pending-banned-account")
    deliveries = {"email": BAN, "session_id": row["session_id"], "video_path": str(video)}
    # Legacy recovery and ancillary delivery ledgers may live outside sidecars.
    files = [root / "recovery" / "pending.json", config.DATA_DIR / "delivery-ledger.json",
             library / "data" / "delivery-ledger.jsonl"]
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(json.dumps(deliveries).encode() + b"\n")
    files += [video, video.with_name(video.name + ".managed.json"),
              config.DATA_DIR / "sidecars" / f"{row['session_id']}.json", Path(row["sidecar_data_path"])]
    before = {path: path.read_bytes() for path in files}
    ban()
    assert {path: path.read_bytes() for path in files} == before
    protection = recovery.media_cleanup_protection()
    assert video in protection["paths"] and digest in protection["sha256"]
    result = media_lifecycle.cleanup_managed_media([video], allowed_roots=(media,))
    assert result["files"] == 0 and not result["errors"]
    assert {path: path.read_bytes() for path in files} == before


def test_ban_migrates_legacy_owner_journal_before_removing_token(local):
    _root, library, media = local
    video, digest = owned(media, "legacy-pending.mp4")
    row = journal(video, digest, "legacy-pending-banned-owner")
    canonical = config.DATA_DIR / "sidecars"
    legacy = library / "data" / "sidecars"
    legacy.mkdir(parents=True, exist_ok=True)
    journal_source = legacy / f"{row['session_id']}.json"
    archive_source = legacy / f"{row['session_id']}.data.zip"
    (canonical / journal_source.name).replace(journal_source)
    Path(row["sidecar_data_path"]).replace(archive_source)
    journal_bytes, archive_bytes = journal_source.read_bytes(), archive_source.read_bytes()

    ban()

    migrated = canonical / journal_source.name
    migrated_archive = canonical / archive_source.name
    assert migrated.is_file() and migrated_archive.is_file()
    migrated_row = json.loads(migrated.read_text(encoding="utf8"))
    assert migrated_row["session_id"] == row["session_id"]
    assert migrated_row["account_email"] == BAN
    assert migrated_archive.read_bytes() == archive_bytes
    assert journal_source.read_bytes() == journal_bytes
    assert archive_source.read_bytes() == archive_bytes
    protection = recovery.media_cleanup_protection()
    assert video in protection["paths"] and digest in protection["sha256"]
    result = media_lifecycle.cleanup_managed_media([video], allowed_roots=(media,))
    assert result["files"] == 0 and not result["errors"]
    assert video.is_file()


def test_legacy_journal_migration_failure_keeps_ban_and_credentials_unwritten(local):
    _root, library, _media = local
    legacy = library / "data" / "sidecars"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "corrupt.json").write_bytes(b'{"session_id":')
    token_path = token_store.record_path(config.SECRETS_DIR, BAN)
    token_before = token_path.read_bytes()
    credential_before = credential_store.record_path(config.SECRETS_DIR, BAN).read_bytes()

    with pytest.raises(RecoveryReadError):
        server._ban_accounts([{"email": BAN, "restriction_confirmed": True,
                               "reason": "fixture confirmed restriction"}])

    assert token_path.read_bytes() == token_before
    assert credential_store.record_path(config.SECRETS_DIR, BAN).read_bytes() == credential_before
    assert not (config.DATA_DIR / "banned_accounts.json").exists()
    assert BAN not in account_bans.banned_emails()


def test_reconcile_rechecks_health_after_campaign_lease_before_banning(local, monkeypatch):
    _root, _library, _media = local
    diagnosis = {"status": "disabled", "issue": {
        "code": "restricted", "restriction_confirmed": True}}
    active = {"status": "active"}
    health = {BAN: diagnosis}
    monkeypatch.setattr(server, "_load_account_health_history", lambda: dict(health))

    @contextmanager
    def lease_with_health_refresh():
        health.clear()
        health[BAN] = active
        yield

    monkeypatch.setattr(server, "campaign_state_lease", lease_with_health_refresh)
    calls = []
    monkeypatch.setattr(server, "_ban_accounts", lambda issues: calls.append(issues))

    assert server._reconcile_account_bans() == []
    assert calls == []
    assert token_store.load(config.SECRETS_DIR, BAN) is not None


@pytest.mark.parametrize("name", ["start_requests.json", "campaign_start_requests.json",
    "original_capture_reservations.json", "sent_videos.json", "sent_reset_history.json",
    "sidecar_migration.json", "campaign_legacy.json", "capture.mp4.managed.json", "capture.mp4.source.json"])
def test_purge_does_not_rewrite_authoritative_documents_even_when_unreadable(local, name):
    _root, library, _media = local
    payload = ('{"email":"' + BAN + '","partial":').encode()
    paths = [config.DATA_DIR / name, library / "data" / name]
    for path in paths:
        path.write_bytes(payload)
    account_bans.purge_local_records({BAN})
    assert all(path.read_bytes() == payload for path in paths)


def test_purge_removes_only_operational_records_next_to_preserved_claims(local):
    _root, library, _media = local
    paths = [config.DATA_DIR / "active_accounts.json", library / "data" / "active_accounts.jsonl"]
    for path in paths:
        path.write_text(json.dumps([{"email": BAN}, {"email": OTHERS[0]}]) + "\n", encoding="utf8")
    account_bans.purge_local_records({BAN})
    assert all(json.loads(path.read_text()) == [{"email": OTHERS[0]}] for path in paths)
