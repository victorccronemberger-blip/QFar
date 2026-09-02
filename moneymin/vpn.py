"""
vpn.py — Detecção de VPN (Windows) para a réplica Android.

O app bloqueia TODA chamada autenticada quando há VPN ativa
(`assertNoVpn` → VpnBlockedError, DETALHAMENTO §4.3). O QMoney roda no
Windows desktop: aqui checamos os adaptadores de rede por nomes típicos de VPN
(WireGuard, Wintun, TAP-, TUN-, OpenVPN, Tailscale, NordVPN, ExpressVPN,
Surfshark, VirtualBox Host-Only, etc.).

Uso:
    from moneymin import vpn
    if vpn.vpn_active():
        # warn/hard-block, como o app

Resultado cacheado por 60 s (o spawn do PowerShell é caro para N contas).
"""
from __future__ import annotations

import os
import subprocess
import time

# Marcadores nos nomes/descrições dos adaptadores de rede. "virtualbox
# host-only" não é VPN real, mas o app Android também enxergaria essa rede
# como não-móvel; mantemos focado em adaptadores de tunelamento real.
_VPN_MARKERS = (
    "wireguard", "wintun", "tap-", "tap ", "tun-", "openvpn", "tailscale",
    "nordvpn", "expressvpn", "surfshark", "protonvpn", "mullvad",
    "cloudflare warp", "warp",
)
_CACHE_SECONDS = 60.0
_cache_ts = 0.0
_cache_value: bool | None = None


def _powershell_query() -> str:
    markers = "|".join(_VPN_MARKERS)
    return (
        "(Get-NetAdapter | Where-Object { "
        f"$_.InterfaceDescription -match '{markers}' "
        "} | Measure-Object).Count"
    )


def _detect() -> bool:
    """Checa adaptadores via PowerShell. False se não der para sondar."""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             _powershell_query()],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:  # noqa: BLE001 — sem powershell/erro → assume sem VPN
        return False
    if proc.returncode != 0:
        return False
    try:
        return int(proc.stdout.strip() or "0") > 0
    except ValueError:
        return False


def vpn_active() -> bool:
    """True se algum adaptador de VPN estiver visível (cache de 60 s)."""
    global _cache_ts, _cache_value
    now = time.monotonic()
    if _cache_value is not None and now - _cache_ts < _CACHE_SECONDS:
        return _cache_value
    value = _detect()
    _cache_value = value
    _cache_ts = now
    return value


def vpn_message() -> str:
    return "VPN ativa detectada no Windows (o app Minute bloquearia toda chamada)."


# env de força: MINUTE_VPN_ENFORCE=1 replica o hard-block do app.
ENFORCE = os.environ.get("MINUTE_VPN_ENFORCE", "").strip() == "1"