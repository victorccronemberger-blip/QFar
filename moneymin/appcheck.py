"""Optional App Check debug provider for an administrator-authorized Firebase app.

No token capture, attestation bypass, or persistent credential cache. Missing
configuration sends no header; a server rejection must remain a rejection.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, tls

logger = logging.getLogger(__name__)
_lock = threading.RLock()
_identity = None
_token = None
_expires = 0.0
_retry_at = 0.0


def _settings():
    return tuple(os.environ.get(key, "").strip() for key in (
        "FIREBASE_APP_CHECK_PROJECT_ID", "FIREBASE_APP_CHECK_APP_ID",
        "FIREBASE_APP_CHECK_API_KEY", "FIREBASE_APP_CHECK_DEBUG_TOKEN"))


def is_app_check_configured() -> bool:
    return all(_settings())


def clear_cache() -> None:
    """Forget process-local credentials. Never read or delete installation files."""
    global _identity, _token, _expires, _retry_at
    with _lock:
        _identity, _token, _expires, _retry_at = None, None, 0.0, 0.0


def _exchange(project, app, api_key, secret):
    path = '/'.join(urllib.parse.quote(part, safe='') for part in (project, app))
    project_path, app_path = path.split('/')
    url = (f"https://firebaseappcheck.googleapis.com/v1/projects/{project_path}"
           f"/apps/{app_path}:exchangeDebugToken?key={urllib.parse.quote(api_key, safe='')}")
    req = urllib.request.Request(url, method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"debugToken": secret}).encode())
    with tls.urlopen(req, timeout=10) as response:
        if response.status != 200:
            raise ValueError("exchange failed")
        raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError("oversized response")
        data = json.loads(raw)
    token, ttl = data.get("token"), data.get("ttl")
    if (not isinstance(token, str) or not token or len(token) > 16384
            or any(ord(c) <= 32 or ord(c) >= 127 for c in token)
            or not isinstance(ttl, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,9})?s", ttl)):
        raise ValueError("invalid token response")
    seconds = float(ttl[:-1])
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("invalid lifetime")
    return token, seconds


def mint_app_check_token(force_refresh: bool = False) -> str | None:
    global _identity, _token, _expires, _retry_at
    with _lock:
        values = _settings()
        identity = hashlib.sha256(json.dumps((str(config.ROOT), values)).encode()).digest()
        if identity != _identity:
            _identity, _token, _expires, _retry_at = identity, None, 0.0, 0.0
        if not all(values):
            # Optional integration: no warnings or network traffic when unconfigured.
            return None
        now = time.monotonic()
        if not force_refresh and _token and now < _expires:
            return _token
        if now < _retry_at:
            return None
        _token, _expires = None, 0.0
        try:
            token, ttl = _exchange(*values)
            # Measure from before the request; latency must not extend the TTL.
            expires = now + ttl - min(300.0, ttl * 0.1)
            if expires <= time.monotonic():
                raise ValueError("expired response")
            _token, _expires = token, expires
            return token
        except Exception:
            # Do not log URLs, response bodies, exceptions or credential values.
            _retry_at = time.monotonic() + 60.0
            logger.warning("App Check indisponível; nova tentativa após 60 segundos. A política do servidor permanece válida.")
            return None


def get_app_check_header() -> dict[str, str]:
    token = mint_app_check_token()
    return {"X-Firebase-AppCheck": token} if token else {}


def is_app_check_rejection(text: str) -> bool:
    """Only explicit markers qualify; a generic 401/403 is not App Check evidence."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return False
    if not isinstance(data, dict):
        return False
    detail = data.get("detail", data)
    if isinstance(detail, dict):
        detail = ' '.join(str(detail.get(k, '')) for k in ('error', 'code', 'message'))
    if not isinstance(detail, str):
        return False
    normalized = re.sub(r"[ _-]", "", detail).casefold()
    return 'appcheck' in normalized
