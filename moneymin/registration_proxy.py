"""Protected proxy pool and per-thread routing for account registration."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from contextvars import ContextVar
import ipaddress
import json
import re
import select
import socket
import socketserver
import threading
import uuid

from . import config, secure_store

_LOCK = threading.RLock()
_ENDPOINT = ContextVar("registration_proxy_endpoint", default=None)
_SECRETS = ContextVar("registration_proxy_secrets", default=())
MAX_BYTES = 256 * 1024
MAX_PROXIES = 500


def endpoint() -> str | None:
    return _ENDPOINT.get()


def redact(value: str, password: str = "") -> str:
    for secret in (*_SECRETS.get(), password):
        if secret:
            value = value.replace(secret, "[credencial protegida]")
    return value


def _load() -> dict:
    value = secure_store.load_secure_settings(config.SECRETS_DIR / "registration_proxies.dat", strict=True)
    if not value:
        return {"items": [], "accounts": {}, "cursor": 0}
    if (set(value) != {"items", "accounts", "cursor"} or not isinstance(value["items"], list)
            or not isinstance(value["accounts"], dict) or type(value["cursor"]) is not int
            or value["cursor"] < 0 or len(value["items"]) > MAX_PROXIES):
        raise ValueError("O cofre de proxies está inválido. Preserve o arquivo e revise a importação.")
    ids = set()
    for row in value["items"]:
        if (not isinstance(row, dict) or set(row) != {"id", "host", "port", "username", "password"}
                or not isinstance(row["id"], str) or not re.fullmatch(r"[a-f0-9]{32}", row["id"])
                or row["id"] in ids):
            raise ValueError("O cofre de proxies está inválido. Preserve o arquivo.")
        _validate(row["host"], row["port"], row["username"], row["password"])
        ids.add(row["id"])
    if any(not isinstance(k, str) or not isinstance(v, str) or v not in ids for k,v in value["accounts"].items()):
        raise ValueError("A associação de proxies está inválida. Preserve o arquivo.")
    return value


def _save(value):
    secure_store.save_secure_settings(config.SECRETS_DIR / "registration_proxies.dat", value)


def _validate(host, port, username, password):
    if (not isinstance(host, str) or not host or len(host) > 253
            or not re.fullmatch(r"[A-Za-z0-9.-]+", host)
            or type(port) is not int or not 1 <= port <= 65535
            or not isinstance(username, str) or not isinstance(password, str)
            or not username or not password or len(username) > 256 or len(password) > 1024
            or any(ord(c) < 32 or ord(c) == 127 for c in username + password)):
        raise ValueError("Proxy inválido; use host:porta:login:senha.")


def public_rows() -> list[dict]:
    with _LOCK:
        return [{"id":p["id"], "label":f'{p["host"]}:{p["port"]}'} for p in _load()["items"]]


def import_text(text: str) -> dict:
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Selecione um arquivo TXT de até 256 KB.")
    parsed = []
    for line_no, raw in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split(":", 3)
        try:
            if len(parts) != 4 or not parts[1].isdigit():
                raise ValueError
            host, port, username, password = parts[0], int(parts[1]), parts[2], parts[3]
            _validate(host, port, username, password)
        except ValueError:
            raise ValueError(f"Linha {line_no} inválida; use host:porta:login:senha. Nenhum proxy foi importado.") from None
        parsed.append({"host":host, "port":port, "username":username, "password":password})
        if len(parsed) > MAX_PROXIES:
            raise ValueError("O arquivo excede o limite de 500 proxies.")
    if not parsed:
        raise ValueError("O arquivo não contém proxies.")
    with _LOCK:
        value = _load()
        added = 0
        for row in parsed:
            previous = next((p for p in value["items"] if (p["host"],p["port"],p["username"]) == (row["host"],row["port"],row["username"])), None)
            if previous:
                previous["password"] = row["password"]
            else:
                value["items"].append({"id":uuid.uuid4().hex, **row}); added += 1
        if len(value["items"]) > MAX_PROXIES:
            raise ValueError("O cofre excederia o limite de 500 proxies; importação preservada.")
        _save(value)
        return {"imported":added, "total":len(value["items"]), "proxies":public_rows()}


def validate_selection(selection):
    if not isinstance(selection, str):
        raise ValueError("Seleção de proxy inválida.")
    rows = public_rows()
    if selection == "":
        return
    if selection == "auto" and rows or any(p["id"] == selection for p in rows):
        return
    raise ValueError("Importe ou selecione um proxy disponível antes de criar a conta.")


def selected(selection: str) -> dict | None:
    validate_selection(selection)
    if not selection:
        return None
    with _LOCK:
        rows = _load()["items"]
        return (rows[0] if selection == "auto" else next(p for p in rows if p["id"] == selection)).copy()


def assign(email: str, selection: str) -> dict | None:
    with _LOCK:
        value = _load()
        existing = value["accounts"].get(email.strip().casefold())
        if existing:
            return next(p.copy() for p in value["items"] if p["id"] == existing)
        if not selection:
            return None
        validate_selection(selection)
        if selection == "auto":
            row = value["items"][value["cursor"] % len(value["items"])]
            value["cursor"] += 1
        else:
            row = next(p for p in value["items"] if p["id"] == selection)
        value["accounts"][email.strip().casefold()] = row["id"]
        _save(value)
        return row.copy()


class TunnelBridge:
    """CONNECT relay: upstream credentials never reach Chrome or public errors."""
    def __init__(self, proxy):
        auth = base64.b64encode((proxy["username"]+":"+proxy["password"]).encode()).decode()
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    self.request.settimeout(30)
                    header = b""
                    while b"\r\n\r\n" not in header and len(header) < 16384:
                        chunk = self.request.recv(4096)
                        if not chunk: return
                        header += chunk
                    parts = header.split(b"\r\n",1)[0].decode("ascii").split()
                    if len(parts) != 3 or parts[0] != "CONNECT":
                        self.request.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n"); return
                    target = parts[1]
                    if not re.fullmatch(r"[A-Za-z0-9.\[\]:-]+", target): return
                    with socket.create_connection((proxy["host"],proxy["port"]),timeout=30) as remote:
                        remote.sendall((f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\nProxy-Authorization: Basic {auth}\r\n\r\n").encode())
                        answer = b""
                        while b"\r\n\r\n" not in answer and len(answer) < 16384:
                            chunk = remote.recv(4096)
                            if not chunk: return
                            answer += chunk
                        if answer.split(b"\r\n",1)[0].split()[1] != b"200":
                            self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n"); return
                        self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                        extra = answer.split(b"\r\n\r\n",1)[1]
                        if extra: self.request.sendall(extra)
                        while True:
                            ready,_,_ = select.select([self.request,remote],[],[],120)
                            if not ready: return
                            for source in ready:
                                data = source.recv(65536)
                                if not data: return
                                (remote if source is self.request else self.request).sendall(data)
                except (OSError,ValueError,IndexError,UnicodeError):
                    return
        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            def handle_error(self, request, address): pass
        self.server = Server(("127.0.0.1",0),Handler)
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)


@contextmanager
def route(proxy):
    if proxy is None:
        token = _ENDPOINT.set(None)
        try:
            yield None
        finally:
            _ENDPOINT.reset(token)
        return
    bridge = TunnelBridge(proxy)
    token = _ENDPOINT.set(bridge.address)
    secrets = _SECRETS.set((proxy["username"], proxy["password"]))
    try:
        yield bridge.address
    finally:
        _SECRETS.reset(secrets)
        _ENDPOINT.reset(token); bridge.close()


def check_exit_ip() -> str:
    from . import tls
    try:
        with tls.urlopen("https://api.ipify.org?format=json", timeout=20) as response:
            ip = json.loads(response.read())["ip"]
        return str(ipaddress.ip_address(ip))
    except Exception:
        raise RuntimeError("Não foi possível confirmar a conexão do proxy. Nenhuma conta foi criada; confira o proxy antes de tentar novamente.") from None
