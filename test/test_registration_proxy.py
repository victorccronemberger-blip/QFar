"""Proxy import, protected persistence and local tunnel tests; no external accounts."""
import base64
import json
import socket
import socketserver
import threading
from unittest.mock import Mock
from urllib.request import Request

import pytest

from moneymin import config, registration_proxy as proxies, tls, transport


@pytest.fixture
def pool(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SECRETS_DIR", tmp_path / "secrets")
    return tmp_path / "secrets" / "registration_proxies.dat"


TXT = "127.0.0.1:8080:fixture-user:fixture-password\n127.0.0.2:8090:second-user:second-password"


def test_import_bom_crlf_deduplicates_and_encrypts_authentication(pool):
    result = proxies.import_text("\ufeff" + TXT.replace("\n", "\r\n") + "\r\n\r\n")
    assert result["total"] == result["imported"] == 2
    assert "fixture-user" not in json.dumps(result)
    assert "fixture-password" not in json.dumps(result)
    assert b"fixture-password" not in pool.read_bytes()
    selected = proxies.assign("first@example.invalid", "auto")
    assert selected["username"] == "fixture-user"
    assert selected["password"] == "fixture-password"
    before = selected["id"]
    result = proxies.import_text(TXT.replace("fixture-password", "replacement-password"))
    assert result["total"] == 2 and result["imported"] == 0
    selected = proxies.assign("FIRST@example.invalid", "auto")
    assert selected["id"] == before and selected["password"] == "replacement-password"


@pytest.mark.parametrize("text", ["", "\n", "host:80:user", "host:no:user:password",
    "host:0:user:password", "host:65536:user:password", "host:80::password",
    "host:80:user:", "https://host:80:user:password", "host:80:user:password\x00",
    "host:80:user:password\ninvalid", "a"*(proxies.MAX_BYTES+1)], ids=["empty", "blank", "missing-password", "nonnumeric-port", "zero-port", "large-port", "empty-login", "empty-password", "url-format", "control-character", "transactional", "oversized"])
def test_invalid_import_preserves_previous_pool_without_printing_auth(pool, text):
    proxies.import_text(TXT)
    before = pool.read_bytes()
    with pytest.raises(ValueError) as error:
        proxies.import_text(text)
    assert "fixture-password" not in str(error.value)
    assert pool.read_bytes() == before


def test_rotation_is_per_account_and_resume_keeps_its_proxy(pool):
    proxies.import_text(TXT)
    first = proxies.assign("a@example.invalid", "auto")
    second = proxies.assign("b@example.invalid", "auto")
    third = proxies.assign("c@example.invalid", "auto")
    assert first["id"] != second["id"] and first["id"] == third["id"]
    assert proxies.assign("a@example.invalid", second["id"])["id"] == first["id"]
    assert proxies.assign("a@example.invalid", "")["id"] == first["id"]
    assert proxies.selected("auto")["id"] == first["id"]
    assert proxies.assign("d@example.invalid", "auto")["id"] == second["id"]
    assert proxies.assign("direct@example.invalid", "") is None


def test_unavailable_selection_and_corrupt_pool_fail_closed(pool):
    with pytest.raises(ValueError):
        proxies.validate_selection("auto")
    proxies.import_text(TXT)
    before = pool.read_bytes()
    with pytest.raises(ValueError):
        proxies.assign("a@example.invalid", "missing")
    assert pool.read_bytes() == before
    pool.write_bytes(b"corrupted protected file")
    with pytest.raises(ValueError):
        proxies.import_text(TXT)
    assert pool.read_bytes() == b"corrupted protected file"


def test_password_can_contain_colons(pool):
    proxies.import_text("host.invalid:443:user:password:with:colons")
    assert proxies.assign("a@example.invalid", "auto")["password"] == "password:with:colons"


def test_connect_tunnel_authenticates_upstream_and_relays_bidirectionally():
    seen = []
    class Upstream(socketserver.BaseRequestHandler):
        def handle(self):
            data = b""
            while b"\r\n\r\n" not in data:
                data += self.request.recv(4096)
            seen.append(data)
            self.request.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
            self.request.sendall(self.request.recv(100).upper())
    upstream = socketserver.ThreadingTCPServer(("127.0.0.1",0), Upstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True); thread.start()
    try:
        proxy = {"host":"127.0.0.1", "port":upstream.server_address[1], "username":"fixture-user", "password":"fixture-password"}
        with proxies.route(proxy):
            endpoint = proxies.endpoint()
            assert "fixture" not in endpoint
            with socket.create_connection(("127.0.0.1",int(endpoint.rsplit(":",1)[1])),timeout=3) as client:
                client.sendall(b"CONNECT destination.invalid:443 HTTP/1.1\r\n\r\n")
                assert client.recv(4096).startswith(b"HTTP/1.1 200")
                client.sendall(b"local-fixture")
                assert client.recv(100) == b"LOCAL-FIXTURE"
            assert proxies.redact("fixture-user fixture-password account-password", "account-password") == "[credencial protegida] [credencial protegida] [credencial protegida]"
            with proxies.route(None):
                assert proxies.endpoint() is None
            assert proxies.endpoint() == endpoint
        assert proxies.endpoint() is None
        assert seen[0].startswith(b"CONNECT destination.invalid:443 ")
        assert base64.b64encode(b"fixture-user:fixture-password") in seen[0]
    finally:
        upstream.shutdown(); upstream.server_close(); thread.join(3)


def test_urllib_selected_proxy_overrides_environment_bypass(monkeypatch):
    monkeypatch.setattr("urllib.request.proxy_bypass", lambda host: True)
    handler = tls._RequiredProxy({"https":"http://127.0.0.1:8080"})
    req = Request("https://destination.invalid/path")
    handler.proxy_open(req,"http://127.0.0.1:8080","https")
    assert req.host == "127.0.0.1:8080" and req._tunnel_host == "destination.invalid"


def test_curl_uses_scoped_proxy_and_overrides_no_proxy(monkeypatch):
    from curl_cffi.const import CurlOpt
    token = proxies._ENDPOINT.set("http://127.0.0.1:8080")
    client = Mock()
    client.request.return_value.status_code = 200
    client.request.return_value.content = b"ok"
    client.request.return_value.headers = {}
    monkeypatch.setattr(transport,"_kind","curl"); monkeypatch.setattr(transport,"_cffi",client)
    try:
        assert transport.http_request("GET","https://destination.invalid") == (200,b"ok")
        options = client.request.call_args.kwargs
        assert options["proxies"] == {"https":"http://127.0.0.1:8080", "http":"http://127.0.0.1:8080"}
        assert options["curl_options"][CurlOpt.NOPROXY] == ""
        assert options["curl_options"][CurlOpt.PROXY] == "http://127.0.0.1:8080"
        with pytest.raises(RuntimeError):
            client.request.side_effect = RuntimeError("connection failed")
            transport.http_request("GET","https://destination.invalid")
        assert client.request.call_count == 2
    finally:
        proxies._ENDPOINT.reset(token)


def test_routing_is_isolated_from_other_threads():
    token = proxies._ENDPOINT.set("http://127.0.0.1:8080")
    observed = []
    thread = threading.Thread(target=lambda: observed.append(proxies.endpoint()))
    try:
        thread.start(); thread.join(3)
        assert observed == [None] and proxies.endpoint().endswith(":8080")
    finally:
        proxies._ENDPOINT.reset(token)
