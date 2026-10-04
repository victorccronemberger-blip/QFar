"""Generic cache, TAR index and health writer integrity with fake offline state.

No real account, media, provider, HTTP range, sensor or registration operation.
"""
from pathlib import Path
import io
import json
import os
import tarfile

import pytest

URL = "https://fixture.example.invalid/artificial.tar"
ETAG = "FICTIONAL-ETAG"
BAD_JSON = {
    "duplicate": b'{"x":1,"x":2}', "nested_duplicate": b'{"x":{"a":1,"a":2}}',
    "escaped_duplicate": b'{"email":1,"em\\u0061il":2}',
    "nan": b'{"x":NaN}', "inf": b'{"x":Infinity}', "negative_inf": b'{"x":-Infinity}',
    "overflow": b'{"x":1e9999}', "integer5000": b'{"x":' + b"9" * 5000 + b"}",
    "recursion": b"[" * 10000 + b"0" + b"]" * 10000,
    "syntax": b"{TRUNCATED", "utf8": b"\xff", "multiple_roots": b"1,2",
}


@pytest.fixture
def local(tmp_path, monkeypatch):
    assert "venom-offline-" in os.environ.get("QMONEY_USER_ROOT", "")
    from moneymin import config, tls
    data = tmp_path / "data"
    data.mkdir()
    for key, value in {"ROOT": tmp_path, "LIBRARY_ROOT": tmp_path, "DATA_DIR": data,
                       "SECRETS_DIR": tmp_path / "secrets", "MEDIA_DATA_DIR": tmp_path / "fake-work"}.items():
        monkeypatch.setattr(config, key, value)
    def forbidden(*args, **kwargs):
        raise AssertionError("Real provider/HTTP/sensor operation forbidden")
    monkeypatch.setattr(tls, "urlopen", forbidden)
    return tmp_path, config


@pytest.mark.parametrize("payload", BAD_JSON.values(), ids=BAD_JSON.keys())
def test_invalid_generic_cache_returns_exact_default_and_preserves_bytes(local, payload):
    root, _ = local
    from moneymin.atomic_io import load_json
    path = root / "cache.json"
    path.write_bytes(payload)
    default = object()
    assert load_json(path, default) is default
    assert path.read_bytes() == payload


@pytest.mark.parametrize("value", [{"x": [1, True, None, "note\u2028inside\u2029string"]}, [], "string", 42, -4,
                                       1.25, True, False, None],
                         ids=["dict", "list", "string", "int", "negative_int", "float", "true", "false", "null"])
def test_generic_cache_preserves_all_valid_json_root_types(local, value):
    root, _ = local
    from moneymin.atomic_io import load_json
    path = root / "cache.json"
    before = b"\xef\xbb\xbf" + json.dumps(value, ensure_ascii=False).encode("utf-8")
    path.write_bytes(before)
    actual = load_json(path, object())
    assert type(actual) is type(value) and actual == value
    assert path.read_bytes() == before


def test_generic_missing_and_unreadable_cache_default_stays_private(local, monkeypatch):
    root, _ = local
    from moneymin.atomic_io import load_json
    path = root / "cache.json"
    default = object()
    assert load_json(path, default) is default
    path.write_bytes(b'{"valid":true}')
    original = Path.read_text
    def unreadable(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError("FICTIONAL-PRIVATE-PATH-PASSWORD")
        return original(candidate, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", unreadable)
    assert load_json(path, default) is default
    assert path.read_bytes() == b'{"valid":true}'


def fake_archive(style="ordinary", nul_type=False):
    stream = io.BytesIO()
    name = "prefix/" + "p" * 105 + "/wanted.bin" if style == "prefix" else "wanted.bin"
    mode = tarfile.PAX_FORMAT if style == "pax" else tarfile.USTAR_FORMAT
    with tarfile.open(fileobj=stream, mode="w", format=mode) as archive:
        for member_name, payload in ((name, b"FIRST"), ("other.bin", b"OTHER")):
            info = tarfile.TarInfo(member_name)
            info.size = len(payload)
            if style == "pax" and member_name == name:
                info.pax_headers = {"comment": "FICTIONAL-PAX-CONTEXT"}
            archive.addfile(info, io.BytesIO(payload))
    content = stream.getvalue()
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
        info = archive.getmember(name)
    member = {"name": name, "offset": info.offset_data, "size": info.size, "typeflag": "0"}
    if nul_type:
        content = bytearray(content)
        position = member["offset"] - 512
        content[position + 156] = 0
        content[position + 148:position + 156] = b" " * 8
        checksum = sum(content[position:position + 512])
        content[position + 148:position + 156] = f"{checksum:06o}\0 ".encode()
        content = bytes(content)
    return content, member


def memory_reader(monkeypatch, module, content, *, fail=False):
    original_copy = module.HttpRangeReader.copy
    reads = []
    class Reader:
        def __init__(self, url):
            assert url == URL
            self.size, self.etag = len(content), ETAG
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, start, length):
            reads.append([start, length])
            if fail:
                raise OSError("FICTIONAL-READER-IO-FAILURE")
            return content[start:start + length]
        def copy(self, member, output, progress=None):
            return original_copy(self, member, output, progress)
    monkeypatch.setattr(module, "HttpRangeReader", Reader)
    return reads


def index_doc(content, member):
    cursor = member["offset"] + ((member["size"] + 511) // 512) * 512
    return {"url": URL, "etag": ETAG, "cursor": cursor, "members": {member["name"]: dict(member)}}


INDEX_FAULTS = ["wrong_url", "wrong_etag", "wrong_archive_size", "cursor_negative", "cursor_unaligned", "cursor_bool",
    "cursor_beyond", "cursor_infinite", "empty_members_positive_cursor", "members_list", "missing_fields", "name_mismatch",
    "offset_string", "offset_bool", "offset_before_header", "offset_unaligned", "size_negative", "size_bool", "payload_beyond",
    "typeflag_not_string", "selected_header_name", "selected_header_size", "selected_header_type"]


@pytest.mark.parametrize("fault", INDEX_FAULTS)
def test_invalid_nested_index_rebuilds_before_trusting_selected_member(local, monkeypatch, fault):
    root, _ = local
    from moneymin import remote_tar as module
    content, member = fake_archive()
    reads = memory_reader(monkeypatch, module, content)
    document = index_doc(content, member)
    cached = document["members"][member["name"]]
    if fault == "wrong_url": document["url"] = "https://other.example.invalid/no.tar"
    elif fault == "wrong_etag": document["etag"] = "OTHER-FICTIONAL-ETAG"
    elif fault == "wrong_archive_size": document["size"] = len(content) + 512
    elif fault == "cursor_negative": document["cursor"] = -512
    elif fault == "cursor_unaligned": document["cursor"] = 1
    elif fault == "cursor_bool": document["cursor"] = True
    elif fault == "cursor_beyond": document["cursor"] = len(content) + 512
    elif fault == "cursor_infinite": document["cursor"] = float("inf")
    elif fault == "empty_members_positive_cursor": document["members"] = {}
    elif fault == "members_list": document["members"] = [1]
    elif fault == "missing_fields": cached.pop("size")
    elif fault == "name_mismatch": cached["name"] = "different.bin"
    elif fault == "offset_string": cached["offset"] = "512"
    elif fault == "offset_bool": cached["offset"] = True
    elif fault == "offset_before_header": cached["offset"] = 0
    elif fault == "offset_unaligned": cached["offset"] = 513
    elif fault == "size_negative": cached["size"] = -1
    elif fault == "size_bool": cached["size"] = True
    elif fault == "payload_beyond": cached["size"] = len(content)
    elif fault == "typeflag_not_string": cached["typeflag"] = None
    elif fault == "selected_header_name": cached["offset"] = 1536
    elif fault == "selected_header_size": cached["size"] = 4
    elif fault == "selected_header_type": cached["typeflag"] = "5"
    path = root / "index.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    archive = module.RemoteTar(URL, path)
    selected = archive.find(lambda value: value.name == member["name"])
    assert selected.offset == member["offset"] and selected.size == 5 and selected.typeflag == "0"
    destination = root / "fictional-payload.bin"
    archive.extract(selected, destination)
    assert destination.read_bytes() == b"FIRST" and reads


@pytest.mark.parametrize("style", ["ordinary", "prefix", "pax"])
@pytest.mark.parametrize("nul_header", [False, True], ids=["type0", "nul_type"])
def test_valid_historical_index_prefix_pax_and_type0_nul_are_compatible(local, monkeypatch, style, nul_header):
    root, _ = local
    from moneymin import remote_tar as module
    content, member = fake_archive(style, nul_header)
    reads = memory_reader(monkeypatch, module, content)
    path = root / "index.json"
    before = json.dumps(index_doc(content, member)).encode()
    path.write_bytes(before)
    found = module.RemoteTar(URL, path).find(lambda value: value.name == member["name"])
    assert found.name == member["name"] and found.offset == member["offset"] and found.size == 5
    assert reads == [[member["offset"] - 512, 512]]
    assert path.read_bytes() == before


def test_selected_header_reader_io_error_is_not_silently_reconstructed(local, monkeypatch):
    root, _ = local
    from moneymin import remote_tar as module
    content, member = fake_archive()
    reads = memory_reader(monkeypatch, module, content, fail=True)
    path = root / "index.json"
    before = json.dumps(index_doc(content, member)).encode()
    path.write_bytes(before)
    with pytest.raises(OSError, match="FICTIONAL-READER-IO-FAILURE"):
        module.RemoteTar(URL, path).find(lambda value: value.name == member["name"])
    assert path.read_bytes() == before and len(reads) == 1


@pytest.mark.parametrize("payload", list(BAD_JSON.values()) + [b"[]"], ids=list(BAD_JSON.keys()) + ["wrong_root"])
def test_unreadable_health_history_is_preserved_without_later_ban_or_preferences(local, monkeypatch, payload):
    _, config = local
    from moneymin.web import server
    path = config.DATA_DIR / "health.json"
    path.write_bytes(payload)
    monkeypatch.setattr(server, "ACCOUNT_HEALTH_PATH", path)
    effects = []
    monkeypatch.setattr(server, "_ban_accounts", lambda *args: effects.append("ban"))
    monkeypatch.setattr(server, "_load_prefs", lambda: {"org_keys": {}})
    monkeypatch.setattr(server, "_save_prefs", lambda *args: effects.append("prefs"))
    result = {"status": "disabled", "checked_at": "FICTIONAL-OBSERVED", "issue": {"restriction_confirmed": True}}
    server._save_account_check("new@example.invalid", result)
    assert path.read_bytes() == payload
    assert result["history_saved"] is False and result["status"] == "disabled"
    assert result["checked_at"] == "FICTIONAL-OBSERVED" and effects == []


def test_valid_health_history_preserves_unrelated_account_and_last_success(local, monkeypatch):
    _, config = local
    from moneymin.web import server
    path = config.DATA_DIR / "health.json"
    old = {"keep@example.invalid": {"last_success_at": "FICTIONAL-OLD"},
           "new@example.invalid": {"last_success_at": "FICTIONAL-LAST-SUCCESS"}}
    path.write_text(json.dumps(old), encoding="utf-8")
    monkeypatch.setattr(server, "ACCOUNT_HEALTH_PATH", path)
    result = {"status": "inconclusive", "checked_at": "FICTIONAL-OBSERVED"}
    server._save_account_check("new@example.invalid", result)
    after = json.loads(path.read_bytes())
    assert after["keep@example.invalid"] == old["keep@example.invalid"]
    assert after["new@example.invalid"]["last_success_at"] == "FICTIONAL-LAST-SUCCESS"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), object()],
                         ids=["nan", "infinity", "negative_infinity", "unserializable"])
def test_invalid_health_publication_candidate_preserves_prior_history_and_remote_result(local, monkeypatch, value):
    _, config = local
    from moneymin.web import server
    path = config.DATA_DIR / "health.json"
    before = b'{"keep@example.invalid":{"last_success_at":"FICTIONAL-OLD"}}'
    path.write_bytes(before)
    monkeypatch.setattr(server, "ACCOUNT_HEALTH_PATH", path)
    effects = []
    monkeypatch.setattr(server, "_ban_accounts", lambda *args: effects.append("ban"))
    monkeypatch.setattr(server, "_load_prefs", lambda: {"org_keys": {}})
    monkeypatch.setattr(server, "_save_prefs", lambda *args: effects.append("prefs"))
    result = {"status": "active", "checked_at": "FICTIONAL-OBSERVED", "org_key": "FICTIONAL-ORG", "meta": value}
    server._save_account_check("new@example.invalid", result)
    assert path.read_bytes() == before and result["history_saved"] is False
    assert result["status"] == "active" and result["checked_at"] == "FICTIONAL-OBSERVED"
    assert result["meta"] is value and effects == []


@pytest.mark.parametrize("cursor", [0, 512], ids=["restart_zero", "inside_known_payload"])
def test_cursor_before_known_padded_end_rebuilds_without_payload_as_header(local, monkeypatch, cursor):
    root, _ = local
    from moneymin import remote_tar as module
    content, member = fake_archive()
    reads = memory_reader(monkeypatch, module, content)
    document = index_doc(content, member)
    document["cursor"] = cursor
    path = root / "index.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    count = module.RemoteTar(URL, path).build_index()
    after = json.loads(path.read_bytes())
    assert count == 2 and set(after["members"]) == {"wanted.bin", "other.bin"}
    assert after["members"]["wanted.bin"] == member
    assert after["members"]["other.bin"] == {"name": "other.bin", "offset": 1536, "size": 5, "typeflag": "0"}
    assert after["cursor"] >= 2048 and reads[0][0] == 0
