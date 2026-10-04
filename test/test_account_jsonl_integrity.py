"""JSONL integrity at credential promotion, generic purge and append boundaries.

All accounts/tokens/passwords are fictional. Run only via the adjacent offline
runner, which supplies a temporary user/library profile and a network guard.
DPAPI is replaced only inside fixtures; no provider or registration is called.
"""
from pathlib import Path
import importlib.util
import json
import os

import pytest

PROJECT = Path(__file__).resolve().parents[1]
KEEP = "keep@example.invalid"
OTHER = "other@example.invalid"
BAN = "ban@example.invalid"
FAKE_PASSWORD = "FICTIONAL-PRIVATE-PASSWORD"
VALID = json.dumps({"email": KEEP, "senha": FAKE_PASSWORD}, ensure_ascii=False).encode("utf-8")
BAD_LINES = {
    "duplicate_password": b'{"email":"other@example.invalid","senha":"FIRST-PRIVATE","senha":"SECOND-PRIVATE"}',
    "duplicate_email": b'{"email":"keep@example.invalid","email":"other@example.invalid","senha":"FAKE-PRIVATE"}',
    "escaped_duplicate": b'{"email":"other@example.invalid","em\\u0061il":"keep@example.invalid","senha":"FAKE-PRIVATE"}',
    "nested_duplicate": b'{"email":"other@example.invalid","senha":"FAKE-PRIVATE","meta":{"x":1,"x":2}}',
    "nan": b'{"email":"other@example.invalid","senha":"FAKE-PRIVATE","meta":NaN}',
    "infinity": b'{"email":"other@example.invalid","senha":"FAKE-PRIVATE","meta":Infinity}',
    "negative_infinity": b'{"email":"other@example.invalid","senha":"FAKE-PRIVATE","meta":-Infinity}',
    "float_overflow": b'{"email":"other@example.invalid","senha":"FAKE-PRIVATE","meta":{"x":1e9999}}',
    "integer_digit_limit": b'{"email":"other@example.invalid","senha":"FAKE-PRIVATE","meta":' + b"9" * 5000 + b"}",
}


def assert_private(text, root):
    for marker in (KEEP, OTHER, BAN, FAKE_PASSWORD, "FIRST-PRIVATE", "SECOND-PRIVATE", "FAKE-PRIVATE", str(root)):
        assert marker not in str(text)


@pytest.fixture
def state(tmp_path, monkeypatch):
    assert "venom-offline-" in os.environ.get("QMONEY_USER_ROOT", "")
    from moneymin import config, credential_store, secure_store, token_store, minute_api, crowtado
    from moneymin.web import server
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
    monkeypatch.setattr(secure_store, "protect_json", lambda value: json.dumps(value).encode("utf-8"))
    monkeypatch.setattr(secure_store, "unprotect_json", lambda payload: json.loads(payload))
    monkeypatch.setattr(server.fx, "usd_brl_quote", lambda: {"available": False})
    def no_provider(*args, **kwargs):
        raise AssertionError("Provider access is forbidden in fictional JSONL tests")
    monkeypatch.setattr(server, "login", no_provider)
    monkeypatch.setattr(minute_api, "_request", no_provider)
    monkeypatch.setattr(crowtado, "login", no_provider)
    monkeypatch.setattr(server.Session, "ensure_auth", no_provider)
    for email in (KEEP, OTHER):
        token_store.record_path(secrets, email).write_text(json.dumps({
            "email": email, "idToken": "FICTIONAL-NOT-A-TOKEN", "refreshToken": "FICTIONAL-NOT-A-REFRESH",
            "localId": "FICTIONAL-UID", "expires_at": 0}), encoding="utf-8")
    return tmp_path, config, server, credential_store, token_store


@pytest.fixture
def registrar(state):
    spec = importlib.util.spec_from_file_location("fictional_jsonl_registrar", PROJECT / "scripts/registrar_conta.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("bad", BAD_LINES.values(), ids=BAD_LINES.keys())
def test_ambiguous_tail_blocks_entire_promotion_and_preserves_bytes(state, bad):
    root, config, server, credentials, _ = state
    from moneymin.atomic_io import JsonStateError
    path = config.DATA_DIR / "contas.jsonl"
    before = VALID + b"\n" + bad + b"\n"
    path.write_bytes(before)
    with pytest.raises(JsonStateError) as error:
        server._configured_crowtado_creds()
    assert_private(error.value, root)
    assert path.read_bytes() == before
    assert not credentials.record_path(config.SECRETS_DIR, KEEP).exists()
    assert not credentials.record_path(config.SECRETS_DIR, OTHER).exists()


def test_http_rejection_is_private_and_does_not_promote_prior_account(state):
    root, config, server, credentials, _ = state
    path = config.DATA_DIR / "contas.jsonl"
    before = VALID + b"\n" + BAD_LINES["nested_duplicate"] + b"\n"
    path.write_bytes(before)
    response = server.create_app(for_testing=True).test_client().get("/api/balances")
    assert response.status_code == 409
    assert response.json["code"] == "local_state_unreadable"
    assert_private(response.get_data(as_text=True), root)
    assert path.read_bytes() == before
    assert not credentials.record_path(config.SECRETS_DIR, KEEP).exists()


def test_complete_history_duplicates_remain_last_row(state):
    _, config, server, _, _ = state
    path = config.DATA_DIR / "contas.jsonl"
    before = VALID + b'\n' + json.dumps({
        "email": KEEP.upper(), "senha": "LATEST-FICTIONAL"}).encode() + b"\n"
    path.write_bytes(before)
    assert server._crowtado_creds()[KEEP] == "LATEST-FICTIONAL"
    assert path.read_bytes() == before


@pytest.mark.parametrize("bad", BAD_LINES.values(), ids=BAD_LINES.keys())
def test_purge_refuses_ambiguous_other_line_without_rewriting_history(state, bad):
    root, config, _, _, _ = state
    from moneymin import account_bans
    from moneymin.atomic_io import JsonStateError
    path = config.DATA_DIR / "contas.jsonl"
    before = bad + b'\n{"email":"ban@example.invalid","senha":"FICTIONAL-BAN"}\n'
    path.write_bytes(before)
    with pytest.raises(JsonStateError) as error:
        account_bans.purge_local_records({BAN})
    assert_private(error.value, root)
    assert path.read_bytes() == before


def test_purge_generic_json_types_and_unicode_string_are_preserved(state):
    _, config, _, _, _ = state
    from moneymin import account_bans
    path = config.DATA_DIR / "generic-history.jsonl"
    rows = [{"email": KEEP}, [KEEP, {"email": BAN}, 2], "note\u2028inside\u2029string", 7, True, None,
            {"email": BAN}]
    path.write_bytes(b"\n".join(json.dumps(row, ensure_ascii=False).encode("utf-8") for row in rows) + b"\n")
    account_bans.purge_local_records({BAN})
    after = [json.loads(line) for line in path.read_bytes().split(b"\n") if line.strip()]
    assert after == [{"email": KEEP}, [KEEP, 2], "note\u2028inside\u2029string", 7, True, None]


@pytest.mark.parametrize("bad", [b"{TRUNCATED", b"\xff"], ids=["syntax", "utf8"])
def test_purge_preserves_unreadable_history_compatibility(state, bad):
    _, config, _, _, _ = state
    from moneymin import account_bans
    path = config.DATA_DIR / "contas.jsonl"
    before = b'{"email":"ban@example.invalid"}\n' + bad + b"\n"
    path.write_bytes(before)
    account_bans.purge_local_records({BAN})
    assert path.read_bytes() == before


def test_ban_refusal_does_not_claim_global_rollback_of_prior_effects(state):
    root, config, server, credentials, tokens = state
    from moneymin import account_bans
    from moneymin.atomic_io import JsonStateError
    credentials.save(config.SECRETS_DIR, KEEP, FAKE_PASSWORD)
    (config.DATA_DIR / "contas.jsonl").write_bytes(VALID + b"\n")
    path = config.DATA_DIR / "zz-generic-history.jsonl"
    before = BAD_LINES["duplicate_password"] + b'\n{"email":"keep@example.invalid","senha":"FICTIONAL"}\n'
    path.write_bytes(before)
    with pytest.raises(JsonStateError):
        server._ban_accounts([{"email": KEEP, "restriction_confirmed": True,
                               "reason": "FICTIONAL-REMOTE-ISSUE", "stage": "FICTIONAL"}])
    assert path.read_bytes() == before
    # This unrelated generic ledger is discovered only by late purge; account
    # removal already persisted and is intentionally not claimed as rolled back.
    assert KEEP in account_bans.banned_emails()
    assert KEEP in server._removed_accounts()
    assert not tokens.record_path(config.SECRETS_DIR, KEEP).exists()
    assert credentials.lookup(config.SECRETS_DIR, KEEP) is None


@pytest.mark.parametrize("suffix", [b"", b"\n", b"\r\n", b"\r"], ids=["none", "lf", "crlf", "cr"])
@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"], ids=["utf8", "bom"])
def test_append_preserves_prefix_and_record_delimiter(state, registrar, suffix, bom):
    _, config, server, _, _ = state
    path = config.DATA_DIR / "contas.jsonl"
    old = json.dumps({"email": KEEP, "senha": FAKE_PASSWORD,
                      "nome": "FICTIONAL \u2028name\u2029 value"}, ensure_ascii=False).encode("utf-8")
    before = bom + old + suffix
    path.write_bytes(before)
    registrar.salvar_conta({"email": OTHER, "senha": "FICTIONAL-NEW"})
    after = path.read_bytes()
    assert after.startswith(before)
    parsed = [json.loads(line.decode("utf-8-sig")) for line in after.split(b"\n") if line.strip()]
    assert [row["email"] for row in parsed] == [KEEP, OTHER]
    assert server._crowtado_creds() == {KEEP: FAKE_PASSWORD, OTHER: "FICTIONAL-NEW"}


@pytest.mark.parametrize("before", [b"", b"\xef\xbb\xbf", b"\n\r\n"], ids=["empty", "bom_only", "blank_lines"])
def test_append_empty_history_preserves_prefix(state, registrar, before):
    _, config, _, _, _ = state
    path = config.DATA_DIR / "contas.jsonl"
    path.write_bytes(before)
    registrar.salvar_conta({"email": OTHER, "senha": "FICTIONAL-NEW"})
    after = path.read_bytes()
    assert after.startswith(before)
    parsed = [json.loads(line.decode("utf-8-sig")) for line in after.split(b"\n") if line.strip()]
    assert [row["email"] for row in parsed] == [OTHER]


@pytest.mark.parametrize("bad", list(BAD_LINES.values()) + [b"{TRUNCATED", b"\xff", b"[]"],
                         ids=list(BAD_LINES.keys()) + ["syntax", "utf8", "wrong_root"])
def test_append_refuses_invalid_history_before_any_write(state, registrar, bad):
    root, config, _, _, _ = state
    from moneymin.atomic_io import JsonStateError
    path = config.DATA_DIR / "contas.jsonl"
    before = VALID + b"\n" + bad + b"\n"
    path.write_bytes(before)
    with pytest.raises(JsonStateError) as error:
        registrar.salvar_conta({"email": OTHER, "senha": "FICTIONAL-NEW"})
    assert_private(error.value, root)
    assert path.read_bytes() == before


@pytest.mark.parametrize("candidate", [[], {"email": OTHER, "senha": "FAKE", "meta": float("nan")},
                                       {"email": OTHER, "senha": "FAKE", "meta": float("inf")},
                                       {"email": OTHER, "senha": "FAKE", "meta": object()}],
                         ids=["wrong_root", "nan", "infinity", "unserializable"])
def test_append_refuses_invalid_candidate_and_preserves_history(state, registrar, candidate):
    root, config, _, _, _ = state
    from moneymin.atomic_io import JsonStateError
    path = config.DATA_DIR / "contas.jsonl"
    path.write_bytes(VALID)
    with pytest.raises(JsonStateError) as error:
        registrar.salvar_conta(candidate)
    assert_private(error.value, root)
    assert path.read_bytes() == VALID


def test_append_missing_history_creates_one_valid_utf8_record(state, registrar):
    _, config, _, _, _ = state
    path = config.DATA_DIR / "contas.jsonl"
    assert not path.exists()
    registrar.salvar_conta({"email": OTHER, "senha": "FICTIONAL-NEW"})
    assert json.loads(path.read_bytes()) == {"email": OTHER, "senha": "FICTIONAL-NEW"}


@pytest.mark.parametrize("tail", [b'{"email":"other@example.invalid","senha":"LATEST', b"\xff", b"[]"],
                         ids=["truncated_syntax_tail", "invalid_utf8_tail", "wrong_account_root"])
def test_unreadable_tail_blocks_global_resolution_before_primary_lookup(state, monkeypatch, tail):
    root, config, server, credentials, _ = state
    from moneymin.atomic_io import JsonStateError
    path = config.DATA_DIR / "contas.jsonl"
    before = VALID + b"\n" + tail + b"\n"
    path.write_bytes(before)
    primary_reads = []
    monkeypatch.setattr(credentials, "load_all", lambda *args: primary_reads.append("load_all") or {})
    with pytest.raises(JsonStateError) as error:
        server._configured_crowtado_creds()
    assert_private(error.value, root)
    assert path.read_bytes() == before
    assert primary_reads == []
    assert not credentials.record_path(config.SECRETS_DIR, KEEP).exists()
    assert not credentials.record_path(config.SECRETS_DIR, OTHER).exists()


def test_consulted_history_io_failure_is_private_and_not_empty(state, monkeypatch):
    root, config, server, credentials, _ = state
    from moneymin.atomic_io import JsonStateError
    path = config.DATA_DIR / "contas.jsonl"
    path.write_bytes(VALID)
    original = Path.read_bytes
    def unreadable(candidate):
        if candidate == path:
            raise PermissionError(str(root) + " FICTIONAL-PRIVATE-PASSWORD")
        return original(candidate)
    monkeypatch.setattr(Path, "read_bytes", unreadable)
    with pytest.raises(JsonStateError) as error:
        server._configured_crowtado_creds()
    assert_private(error.value, root)
    assert original(path) == VALID
    assert not credentials.record_path(config.SECRETS_DIR, KEEP).exists()


def test_legitimately_missing_history_remains_absent(state):
    _, config, server, _, _ = state
    assert not (config.DATA_DIR / "contas.jsonl").exists()
    assert server._crowtado_creds() == {}


@pytest.mark.parametrize("separator", ["\u2028", "\u2029"], ids=["line_separator", "paragraph_separator"])
def test_unicode_separator_between_objects_is_not_a_valid_jsonl_boundary(state, registrar, separator):
    root, config, _, _, _ = state
    from moneymin.atomic_io import JsonStateError
    path = config.DATA_DIR / "contas.jsonl"
    before = VALID + separator.encode("utf-8") + VALID
    path.write_bytes(before)
    with pytest.raises(JsonStateError) as error:
        registrar.salvar_conta({"email": OTHER, "senha": "FICTIONAL-NEW"})
    assert_private(error.value, root)
    assert path.read_bytes() == before


def test_noninitial_bom_blocks_global_resolution_and_preserves_history(state):
    root, config, server, credentials, _ = state
    from moneymin.atomic_io import JsonStateError
    path = config.DATA_DIR / "contas.jsonl"
    before = VALID + b"\n\xef\xbb\xbf" + VALID + b"\n"
    path.write_bytes(before)
    with pytest.raises(JsonStateError) as error:
        server._configured_crowtado_creds()
    assert_private(error.value, root)
    assert path.read_bytes() == before
    assert not credentials.record_path(config.SECRETS_DIR, KEEP).exists()


def test_noninitial_bom_remains_unreadable_to_generic_purge(state):
    _, config, _, _, _ = state
    from moneymin import account_bans
    path = config.DATA_DIR / "generic-history.jsonl"
    before = VALID + b'\n\xef\xbb\xbf{"email":"ban@example.invalid"}\n'
    path.write_bytes(before)
    account_bans.purge_local_records({BAN})
    assert path.read_bytes() == before


@pytest.mark.parametrize("suffix", [b"", b"\n", b"\r\n", b"\r"], ids=["none", "lf", "crlf", "cr"])
def test_double_initial_bom_remains_unreadable_to_generic_purge(state, suffix):
    _, config, _, _, _ = state
    from moneymin import account_bans
    path = config.DATA_DIR / "generic-history.jsonl"
    before = b'\xef\xbb\xbf\xef\xbb\xbf{"email":"ban@example.invalid"}' + suffix
    path.write_bytes(before)
    account_bans.purge_local_records({BAN})
    assert path.read_bytes() == before


@pytest.mark.parametrize("bad", BAD_LINES.values(), ids=BAD_LINES.keys())
def test_generic_json_ambiguity_is_private_before_scrub_rewrite(state, bad):
    root, config, _, _, _ = state
    from moneymin import account_bans
    from moneymin.atomic_io import JsonStateError
    path = config.DATA_DIR / "generic-state.json"
    before = b'{"retained":' + bad + b',"removed":{"email":"ban@example.invalid"}}'
    path.write_bytes(before)
    with pytest.raises(JsonStateError) as refusal:
        account_bans.purge_local_records({BAN})
    assert_private(refusal.value, root)
    assert path.read_bytes() == before


@pytest.mark.parametrize("before", [b"{TRUNCATED", b"\xff", b'{"x":1},{"y":2}', b"1 2", b""],
                         ids=["syntax", "utf8", "comma_roots", "space_roots", "empty"])
def test_generic_json_unreadable_or_multiple_roots_are_preserved(state, before):
    _, config, _, _, _ = state
    from moneymin import account_bans
    path = config.DATA_DIR / "generic-state.json"
    path.write_bytes(before)
    account_bans.purge_local_records({BAN})
    assert path.read_bytes() == before


@pytest.mark.parametrize("value,expected", [
    ({"retained": {"email": KEEP}, "removed": {"email": BAN}}, {"retained": {"email": KEEP}}),
    ([{"email": KEEP}, {"email": BAN}, "note\u2028and\u2029value"], [{"email": KEEP}, "note\u2028and\u2029value"]),
    ("ordinary string", "ordinary string"), (42, 42), (1.25, 1.25), (True, True), (None, None)],
    ids=["dict", "list", "string", "integer", "float", "boolean", "null"])
def test_generic_json_valid_roots_retain_existing_scrub_contract(state, value, expected):
    _, config, _, _, _ = state
    from moneymin import account_bans
    path = config.DATA_DIR / "generic-state.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(value, ensure_ascii=False).encode("utf-8"))
    account_bans.purge_local_records({BAN})
    assert json.loads(path.read_text(encoding="utf-8-sig")) == expected


def test_generic_json_removed_string_root_is_deleted_as_before(state):
    _, config, _, _, _ = state
    from moneymin import account_bans
    path = config.DATA_DIR / "generic-state.json"
    path.write_text(json.dumps(BAN), encoding="utf-8")
    account_bans.purge_local_records({BAN})
    assert not path.exists()
