"""Authoritative budget/health and verified IMU cache with fictional offline state."""
from pathlib import Path
import csv
import hashlib
import io
import json
import os

import pytest


@pytest.fixture
def local(tmp_path, monkeypatch):
    assert "venom-offline-" in os.environ.get("QMONEY_USER_ROOT", "")
    from moneymin import config, tls
    data = tmp_path / "data"
    data.mkdir()
    for key, value in {"ROOT": tmp_path, "LIBRARY_ROOT": tmp_path, "DATA_DIR": data,
                       "MEDIA_DATA_DIR": tmp_path / "fake-work", "SECRETS_DIR": tmp_path / "fake-secrets"}.items():
        monkeypatch.setattr(config, key, value)
    def forbidden(*args, **kwargs):
        raise AssertionError("Real account/provider/media/sensor operation forbidden")
    monkeypatch.setattr(tls, "urlopen", forbidden)
    return tmp_path, config, forbidden


@pytest.mark.parametrize("document", [{"blocks": True}, {"blocks": False}, {"blocks": 1.9},
    {"blocks": "2"}, {"blocks": -1}, {"budget_gb": True}, {"budget_gb": 1.9},
    {"budget_gb": -1}, {}, [], {"budget_gb": None}],
    ids=["blocks_bool_true", "blocks_bool_false", "blocks_fraction", "blocks_string", "blocks_negative",
         "GB_bool", "GB_fraction", "GB_negative", "missing_value", "wrong_root", "GB_null"])
def test_invalid_consulted_budget_is_private_and_preserved(local, document):
    _, _, _ = local
    from moneymin import ego_accelerator
    from moneymin.atomic_io import JsonStateError
    path = ego_accelerator.budget_path()
    before = json.dumps(document).encode()
    path.write_bytes(before)
    for default in (0, 400):
        with pytest.raises(JsonStateError) as error:
            ego_accelerator.configured_budget_gb(default)
        assert "blocks" not in str(error.value) and "budget_gb" not in str(error.value)
    assert path.read_bytes() == before


@pytest.mark.parametrize("before", [b'{"budget_gb":0,"budget_gb":600}', b'{"budget_gb":600,"tail":',
    b'{"budget_gb":NaN}', b'{"budget_gb":1e999}', b"\xff"],
    ids=["duplicate", "syntax_tail", "NaN", "overflow", "UTF8"])
def test_unreadable_budget_never_becomes_caller_default(local, before):
    from moneymin import ego_accelerator
    from moneymin.atomic_io import JsonStateError
    path = ego_accelerator.budget_path()
    path.write_bytes(before)
    with pytest.raises(JsonStateError):
        ego_accelerator.configured_budget_gb(400)
    assert path.read_bytes() == before


@pytest.mark.parametrize("document,expected", [({"blocks": 0}, 0), ({"blocks": 2}, 1000),
    ({"budget_gb": 0}, 0), ({"budget_gb": 400}, 400), ({"budget_gb": "400"}, 400), ({"budget_gb": 400.0}, 400)])
def test_valid_budget_and_legacy_settings_keep_compatibility(local, document, expected):
    from moneymin import ego_accelerator
    path = ego_accelerator.budget_path()
    before = json.dumps(document).encode()
    path.write_bytes(before)
    assert ego_accelerator.configured_budget_gb() == expected
    assert path.read_bytes() == before


def test_only_absent_budget_receives_default(local):
    from moneymin import ego_accelerator
    assert not ego_accelerator.budget_path().exists()
    assert ego_accelerator.configured_budget_gb() == 0
    assert ego_accelerator.configured_budget_gb(400) == 400


@pytest.mark.parametrize("bad", [None, [], "string", {"email": "other@example.invalid"}, {"email": None},
    {"issue": []}, {"issue": {"email": "other@example.invalid"}}, {"issue": {"restriction_confirmed": 1}},
    {"issue": {"retryable": "yes"}}, {"status": []}, {"checked_at": 123}, {"last_success_at": {}},
    {"history_saved": "false"}, {"permanently_removed": 1}],
    ids=["null", "list", "string", "wrong_owner", "email_null", "issue_list", "issue_wrong_owner",
         "restriction_number", "retryable_string", "status_list", "checked_number", "last_success_dict",
         "history_string", "removed_number"])
def test_later_invalid_health_record_vetoes_before_token_iteration(local, monkeypatch, bad):
    _, config, _ = local
    from moneymin.web import server
    from moneymin.atomic_io import JsonStateError
    path = config.DATA_DIR / "fictional-health.json"
    document = {"a@example.invalid": {"status": "inconclusive"}, "later@example.invalid": bad}
    before = json.dumps(document).encode()
    path.write_bytes(before)
    monkeypatch.setattr(server, "ACCOUNT_HEALTH_PATH", path)
    monkeypatch.setattr(server, "_load_prefs", lambda: {})
    calls = []
    monkeypatch.setattr(server.token_store, "records", lambda *args: calls.append("records") or {})
    with pytest.raises(JsonStateError) as error:
        server._list_accounts()
    assert "example.invalid" not in str(error.value) and calls == []
    assert path.read_bytes() == before


def test_normalized_health_owner_collision_is_private(local, monkeypatch):
    _, config, _ = local
    from moneymin.web import server
    from moneymin.atomic_io import JsonStateError
    path = config.DATA_DIR / "fictional-health.json"
    before = b'{"A@example.invalid":{"status":"disabled"},"a@example.invalid":{"status":"active"}}'
    path.write_bytes(before)
    monkeypatch.setattr(server, "ACCOUNT_HEALTH_PATH", path)
    monkeypatch.setattr(server, "_load_prefs", lambda: {})
    monkeypatch.setattr(server.token_store, "records", lambda *args: {})
    with pytest.raises(JsonStateError):
        server._list_accounts()
    assert path.read_bytes() == before


@pytest.mark.parametrize("record", [{"status": "inconclusive"},
    {"email": " A@example.invalid ", "status": "active", "checked_at": "FICTIONAL", "last_success_at": None},
    {"issue": {"restriction_confirmed": False, "retryable": True}, "history_saved": False}],
    ids=["legacy_no_email", "canonical_owner", "known_issue_flags"])
def test_health_legacy_and_optional_owner_records_remain_diagnostic(local, monkeypatch, record):
    _, config, _ = local
    from moneymin.web import server
    path = config.DATA_DIR / "fictional-health.json"
    before = json.dumps({" A@example.invalid ": record}).encode()
    path.write_bytes(before)
    monkeypatch.setattr(server, "ACCOUNT_HEALTH_PATH", path)
    monkeypatch.setattr(server, "_load_prefs", lambda: {"org_keys": {}})
    monkeypatch.setattr(server, "_removed_accounts", lambda: set())
    monkeypatch.setattr(server.token_store, "records", lambda *args: {"fixture":
        (config.DATA_DIR / "not-read-token.json", {"email": "a@example.invalid", "expires_at": 0})})
    actual = server._list_accounts()
    assert actual[0]["email"] == "a@example.invalid" and actual[0]["last_check"] == record
    assert path.read_bytes() == before


@pytest.mark.parametrize("attempts", [True, False, 1.5, "2", -1, None],
    ids=["true", "false", "fraction", "string", "negative", "null"])
def test_known_attempts_type_refused_before_health_publication(local, monkeypatch, attempts):
    _, config, _ = local
    from moneymin.web import server
    from moneymin.atomic_io import JsonStateError
    path = config.DATA_DIR / "fictional-attempts-health.json"
    document = {"earlier@example.invalid": {"status": "inconclusive"},
                "later@example.invalid": {"status": "active", "attempts": attempts}}
    before = json.dumps(document).encode()
    path.write_bytes(before)
    monkeypatch.setattr(server, "ACCOUNT_HEALTH_PATH", path)
    monkeypatch.setattr(server, "_load_prefs", lambda: {})
    calls = []
    monkeypatch.setattr(server.token_store, "records", lambda *args: calls.append("records") or {})
    with pytest.raises(JsonStateError) as error:
        server._list_accounts()
    assert "example.invalid" not in str(error.value) and "attempts" not in str(error.value)
    assert calls == [] and path.read_bytes() == before


@pytest.mark.parametrize("record", [{"status": "inconclusive"}, {"attempts": 0},
    {"attempts": 2, "future_extension": [None, {"finite": 1.25}]}],
    ids=["missing", "zero", "positive_extension"])
def test_optional_integer_attempts_and_extensions_keep_compatibility(local, monkeypatch, record):
    _, config, _ = local
    from moneymin.web import server
    path = config.DATA_DIR / "fictional-attempts-health.json"
    before = json.dumps({" A@example.invalid ": record}).encode()
    path.write_bytes(before)
    monkeypatch.setattr(server, "ACCOUNT_HEALTH_PATH", path)
    monkeypatch.setattr(server, "_load_prefs", lambda: {"org_keys": {}})
    monkeypatch.setattr(server, "_removed_accounts", lambda: set())
    monkeypatch.setattr(server.token_store, "records", lambda *args: {"fixture":
        (config.DATA_DIR / "not-read-token.json", {"email": "a@example.invalid", "expires_at": 0})})
    actual = server._list_accounts()
    assert actual[0]["email"] == "a@example.invalid" and actual[0]["last_check"] == record
    assert path.read_bytes() == before


def source_csv(root, continuous=False, newline="\r\n"):
    path = root / "FICTIONAL_parent_imu.csv"
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator=newline)
    writer.writerow(["canonical_timestamp_ms", "gyro_x", "gyro_y", "gyro_z", "accl_x", "accl_y", "accl_z"])
    for stamp in reversed(list(range(0, 1011, 10)) if continuous else [0, 10, 1000, 1010]):
        writer.writerow([stamp, 1, 2, 3, 4, 5, 6])
    raw = stream.getvalue().encode()
    path.write_bytes(raw)
    return path, raw


def original_clip():
    return {"clip_uid": "FICTIONAL-original", "source": "ego4d", "parent_video_uid": "FICTIONAL_parent",
            "window_s": [0, 1.01], "dedup_clip_uids": ["FICTIONAL-official"]}


def cache_path(config, source):
    return config.DATA_DIR / "imu_coverage" / (hashlib.sha256(str(source.resolve()).encode()).hexdigest() + ".json")


@pytest.mark.parametrize("forged", [[[0, 1.01]], [], [[False, True]], [[1, 1.01], [0, .01]]],
    ids=["gap_with_correct_identity", "empty_list", "bool_endpoints", "out_of_order"])
def test_finite_forged_intervals_with_unchanged_source_identity_are_reconstructed(local, monkeypatch, forged):
    root, config, forbidden = local
    from moneymin import imu_coverage
    source, raw = source_csv(root, continuous=forged == [])
    monkeypatch.setattr(imu_coverage.ego4d, "find_clip", forbidden)
    expected = [original_clip()] if forged == [] else []
    assert imu_coverage.refine_candidates([original_clip()], root, .5, 2) == expected
    path = cache_path(config, source)
    document = json.loads(path.read_bytes())
    original_identity = dict(document["source"])
    document["intervals"] = forged
    document["content_sha256"] = hashlib.sha256(raw).hexdigest()
    before = json.dumps(document).encode()
    path.write_bytes(before)
    imu_coverage._intervals.cache_clear()
    canonical = getattr(imu_coverage, "_canonical_intervals", None)
    if canonical is not None:
        canonical.cache_clear()  # Simulate first use with no prior trusted canonical result.
    actual = imu_coverage.refine_candidates([original_clip()], root, .5, 2)
    assert actual == expected
    after = json.loads(path.read_bytes())
    assert after["source"] == original_identity
    assert after["intervals"] != forged


def test_same_stat_source_change_invalidates_reader_lru_and_persisted_cache(local, monkeypatch):
    root, _, forbidden = local
    from moneymin import imu_coverage
    source, before = source_csv(root, continuous=True)
    stat = source.stat()
    monkeypatch.setattr(imu_coverage.ego4d, "find_clip", forbidden)
    assert imu_coverage.refine_candidates([original_clip()], root, .5, 2) == [original_clip()]
    lines = before.decode().splitlines(keepends=True)
    for i in range(1, len(lines)):
        fields = lines[i].split(",")
        if 10 < int(fields[0]) < 1000:
            fields[1] = "X"
            lines[i] = ",".join(fields)
    changed = "".join(lines).encode()
    assert len(changed) == len(before)
    source.write_bytes(changed)
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert source.stat().st_mtime_ns == stat.st_mtime_ns
    # Deliberately retain reader LRU: fingerprint must precede its key lookup.
    assert imu_coverage.refine_candidates([original_clip()], root, .5, 2) == []


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"], ids=["LF", "CRLF", "CR"])
def test_valid_cache_reuses_verified_canonical_result_without_full_csv_capture(local, monkeypatch, newline):
    root, config, forbidden = local
    from moneymin import imu_coverage
    source, raw = source_csv(root, continuous=True, newline=newline)
    monkeypatch.setattr(imu_coverage.ego4d, "find_clip", forbidden)
    assert imu_coverage.refine_candidates([original_clip()], root, .5, 2) == [original_clip()]
    path = cache_path(config, source)
    before = path.read_bytes()
    imu_coverage._intervals.cache_clear()
    monkeypatch.setattr(imu_coverage.csv, "DictReader", forbidden)
    original = Path.read_bytes
    def source_capture_forbidden(candidate):
        assert candidate != source, "Production source fingerprint may not capture a complete CSV"
        return original(candidate)
    monkeypatch.setattr(Path, "read_bytes", source_capture_forbidden)
    assert imu_coverage.refine_candidates([original_clip()], root, .5, 2) == [original_clip()]
    assert path.read_bytes() == before
    assert hashlib.sha256(raw).hexdigest() == json.loads(before)["source"]["sha256"]
