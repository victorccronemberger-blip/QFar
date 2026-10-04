"""Run isolated, offline tests from any checkout or CI interpreter.

No arguments runs pytest; ``--pytest [args]`` selects pytest explicitly.
Existing arguments remain unittest arguments; ``--unittest`` is also accepted.
The Python network guard is supplementary, not an operating-system firewall.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile


OFFLINE_GUARD = r'''
import ipaddress
import socket
from urllib.parse import urlsplit

def _local(host):
    if isinstance(host, bytes):
        try:
            host = host.decode('ascii')
        except UnicodeError:
            return False
    if str(host).lower() == 'localhost':
        return True
    try:
        return ipaddress.ip_address(str(host)).is_loopback
    except ValueError:
        return False

def _address(address):
    if not isinstance(address, tuple) or not address or not _local(address[0]):
        raise OSError('External network is disabled by the offline test runner.')

_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_sendto = socket.socket.sendto
def connect(self, address):
    _address(address)
    return _connect(self, address)
def connect_ex(self, address):
    _address(address)
    return _connect_ex(self, address)
def sendto(self, data, *args):
    _address(args[-1] if args else None)
    return _sendto(self, data, *args)
socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
socket.socket.sendto = sendto

_getaddrinfo = socket.getaddrinfo
_gethostbyname = socket.gethostbyname
_gethostbyname_ex = socket.gethostbyname_ex
_gethostbyaddr = socket.gethostbyaddr
_getnameinfo = socket.getnameinfo
def getaddrinfo(host, *args, **kwargs):
    if host is not None and not _local(host):
        raise OSError('External DNS is disabled by the offline test runner.')
    return _getaddrinfo(host, *args, **kwargs)
def _dns(original):
    def resolve(host, *args, **kwargs):
        if not _local(host):
            raise OSError('External DNS is disabled by the offline test runner.')
        return original(host, *args, **kwargs)
    return resolve
def getnameinfo(address, *args, **kwargs):
    _address(address)
    return _getnameinfo(address, *args, **kwargs)
socket.getaddrinfo = getaddrinfo
socket.gethostbyname = _dns(_gethostbyname)
socket.gethostbyname_ex = _dns(_gethostbyname_ex)
socket.gethostbyaddr = _dns(_gethostbyaddr)
socket.getnameinfo = getnameinfo

def guard_http(cls):
    original = cls.request
    def request(self, method, url, *args, **kwargs):
        if not _local(urlsplit(str(url)).hostname):
            raise OSError('External HTTP is disabled by the offline test runner.')
        return original(self, method, url, *args, **kwargs)
    cls.request = request

try:
    import requests.sessions
    guard_http(requests.sessions.Session)
except ImportError:
    pass
try:
    import curl_cffi.requests
    guard_http(curl_cffi.requests.Session)
    guard_http(curl_cffi.requests.AsyncSession)
except ImportError:
    pass
'''


def test_command(arguments: list[str]) -> list[str]:
    if not arguments:
        module, arguments = "pytest", ["-q", "test"]
    elif arguments[0] == "--pytest":
        module, arguments = "pytest", arguments[1:] or ["-q", "test"]
    else:
        module = "unittest"
        if arguments[0] == "--unittest":
            arguments = arguments[1:] or ["discover", "-s", "test", "-q"]
    return [sys.executable, "-B", "-X", "utf8", "-m", module, *arguments]


def main() -> int:
    project = Path(__file__).resolve().parents[1]
    command = test_command(sys.argv[1:])
    with tempfile.TemporaryDirectory(prefix="venom-offline-") as folder:
        root = Path(folder)
        guard_dir, runtime = root / "guard", root / "runtime"
        guard_dir.mkdir()
        runtime.mkdir()
        (guard_dir / "sitecustomize.py").write_text(OFFLINE_GUARD, encoding="utf-8")
        for name in ("aws-credentials", "aws-config", "boto-config"):
            (root / name).write_text("", encoding="utf-8")
        private_prefixes = ("QMONEY_", "MINUTE_", "AWS_", "HOSTINGER_", "EGO4D_",
                            "CROWTADO_", "CLARU_")
        excluded_keys = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                         "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE",
                         "GOOGLE_APPLICATION_CREDENTIALS", "BOTO_CONFIG",
                         "PLAYWRIGHT_BROWSERS_PATH", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"}
        environment = {key: value for key, value in os.environ.items()
                       if not key.upper().startswith(private_prefixes)
                       and key.upper() not in excluded_keys}
        environment.update(
            QMONEY_USER_ROOT=str(root), QMONEY_LIBRARY_ROOT=str(root),
            QMONEY_RUNTIME_ROOT=str(runtime), MINUTE_VPN_ENFORCE="0",
            MINUTE_REQUIRE_CURL="0", MINUTE_PUBLISH_APP_OPENED="0",
            AWS_SHARED_CREDENTIALS_FILE=str(root / "aws-credentials"),
            AWS_CONFIG_FILE=str(root / "aws-config"), BOTO_CONFIG=str(root / "boto-config"),
            AWS_EC2_METADATA_DISABLED="true", PYTHONIOENCODING="utf-8",
            PYTHONPATH=os.pathsep.join((str(guard_dir), str(project))),
            PYTHONNOUSERSITE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        )
        return subprocess.run(
            command, cwd=project, env=environment,
        ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
