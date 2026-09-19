"""Defaults de teste: VPN/TLS curl não devem derrubar a suíte isolada."""
from __future__ import annotations

import pytest

from moneymin import config, vpn


@pytest.fixture(autouse=True)
def _relax_local_transport_gates(monkeypatch):
    monkeypatch.setattr(vpn, "ENFORCE", False)
    monkeypatch.setattr(config, "REQUIRE_CURL", False)
