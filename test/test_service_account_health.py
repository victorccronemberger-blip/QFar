"""Both providers are simulated; no account creation, uploads or withdrawals."""
import json
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from moneymin import config, crowtado, minute_api, registration_proxy
from moneymin.web import account_health, server, wallet

EMAIL = "fixture@example.invalid"


@pytest.fixture
def local(tmp_path, monkeypatch):
    for name in ("ACCOUNT_HEALTH_PATH", "PREFS_PATH"):
        monkeypatch.setattr(server, name, tmp_path / (name + ".json"))
    monkeypatch.setattr(server.time, "sleep", lambda *_: None)
    monkeypatch.setattr(registration_proxy, "assign", lambda *_: None)
    monkeypatch.setattr(server, "_saved_account_password", lambda *_: "fixture-secret-password")
    archive = Mock()
    monkeypatch.setattr(server, "_ban_accounts", archive)
    session = Mock(data={"expires_at": 123})
    session.ensure_auth.return_value = {"disabled": False, "organizations": [
        {"resourceKey": config.ORG_KEY, "disabled": False}]}
    session.checked_quality_state.return_value = {"userState": "active"}
    monkeypatch.setattr(server.Session, "from_email", Mock(return_value=session))
    probe = Mock(return_value={"restricted": False, "payout_available": True})
    monkeypatch.setattr(crowtado, "verificar_restricoes", probe)
    return session, probe, archive


@pytest.mark.parametrize("minute", ["active", "restricted", "unknown"])
@pytest.mark.parametrize("site", ["active", "restricted", "unknown"])
def test_independent_service_matrix(local, minute, site):
    session, probe, archive = local
    if minute != "active":
        session.ensure_auth.side_effect = minute_api.AuthError(
            "fixture-private-token", code="restricted" if minute == "restricted" else "service")
    if site != "active":
        probe.side_effect = crowtado.CrowtadoError(
            "fixture-private-token", code="restricted" if site == "restricted" else "service")
    row = server._check_account_health(EMAIL)
    assert row["providers"]["minute"]["status"] == {"restricted": "disabled", "unknown": "inconclusive"}.get(minute, "active")
    assert row["providers"]["crowtado"]["status"] == {"restricted": "disabled", "unknown": "inconclusive"}.get(site, "active")
    assert row["restricted_providers"] == [name for name, state in (("minute", minute), ("crowtado", site)) if state == "restricted"]
    assert (row["status"] == "active") == (minute == site == "active")
    assert probe.called and session.ensure_auth.called
    archive.assert_not_called()
    assert "permanently_removed" not in row
    assert "fixture-private-token" not in json.dumps(row)
    assert "fixture-secret-password" not in json.dumps(row)
    assert server._load_account_health_history()[EMAIL]["providers"] == row["providers"]


def test_crowtado_payout_hold_does_not_change_minute_or_claim_login_ban(local):
    session, probe, archive = local
    probe.return_value = {"restricted": True, "restriction_kind": "payout", "reason": "A Crowtado reteve os saques."}
    row = server._check_account_health(EMAIL)
    assert row["providers"]["minute"]["status"] == "active"
    assert row["providers"]["crowtado"]["restriction_kind"] == "payout"
    assert row["restricted_providers"] == ["crowtado"]
    archive.assert_not_called()
    assert server._load_prefs()["org_keys"][EMAIL] == config.ORG_KEY


def test_missing_crowtado_password_does_not_clear_or_disable_minute(local, monkeypatch):
    monkeypatch.setattr(server, "_saved_account_password", lambda *_: None)
    row = server._check_account_health(EMAIL)
    assert row["providers"]["minute"]["status"] == "active"
    assert row["providers"]["crowtado"]["status"] == "needs_reauth"
    assert row["restricted_providers"] == []
    assert row["status"] != "active"
    local[1].assert_not_called()
    assert "Crowtado" in row["issue"]["action"]


def test_history_keeps_provider_restriction_during_failure_until_explicit_clear(local):
    session, probe, _ = local
    probe.side_effect = crowtado.CrowtadoError("banned", code="restricted")
    first = server._check_account_health(EMAIL)
    probe.side_effect = crowtado.CrowtadoError("timeout", code="timeout")
    failed = server._check_account_health(EMAIL)
    assert failed["providers"]["minute"]["status"] == "active"
    assert failed["providers"]["crowtado"]["status"] == "inconclusive"
    assert failed["providers"]["crowtado"]["last_restriction"]["checked_at"] == first["providers"]["crowtado"]["checked_at"]
    assert "last_restriction" not in failed["providers"]["minute"]
    state = wallet.restriction({}, wallet.reading({}), {"last_check": failed})
    assert state["code"] == "historical_restriction" and not state["confirmed"]
    probe.side_effect = None
    cleared = server._check_account_health(EMAIL)
    assert cleared["status"] == "active"
    assert "last_restriction" not in cleared["providers"]["crowtado"]


def test_legacy_success_is_minute_only(local):
    server._save_account_check(EMAIL, {"status": "active", "checked_at": "2026-01-01T00:00:00Z"})
    local[0].ensure_auth.side_effect = TimeoutError("timeout")
    local[1].side_effect = TimeoutError("timeout")
    row = server._check_account_health(EMAIL)
    assert row["providers"]["minute"]["last_success_at"] == "2026-01-01T00:00:00Z"
    assert row["providers"]["crowtado"]["last_success_at"] is None


def test_proxy_failure_has_no_direct_fallback_or_false_clear(local, monkeypatch):
    monkeypatch.setattr(registration_proxy, "assign", Mock(side_effect=ValueError("fixture-private-proxy-password")))
    row = server._check_account_health(EMAIL)
    assert row["status"] == "inconclusive"
    assert row["pending_providers"] == ["minute", "crowtado"]
    server.Session.from_email.assert_not_called()
    local[1].assert_not_called()
    assert "fixture-private-proxy-password" not in json.dumps(row)


def test_claru_only_checks_minute(local):
    email = "fixture@supply.claru.ai"
    local[0].ensure_auth.return_value["organizations"] = [{"resourceKey": config.CLARU_ORG_KEY}]
    row = server._check_account_health(email)
    assert row["status"] == "active"
    assert row["providers"]["crowtado"]["status"] == "not_applicable"
    local[1].assert_not_called()


@pytest.mark.parametrize("quality", [{}, {"userState": "unknown"}, {"userState": True}])
def test_unknown_minute_quality_is_inconclusive_with_other_service_still_checked(local, quality):
    local[0].checked_quality_state.return_value = quality
    row = server._check_account_health(EMAIL)
    assert row["providers"]["minute"]["status"] == "inconclusive"
    assert row["providers"]["crowtado"]["status"] == "active"


@pytest.mark.parametrize("quality", ["on_hold", "inactive"])
def test_minute_quality_restriction_is_provider_specific(local, quality):
    local[0].checked_quality_state.return_value = {"userState": quality}
    row = server._check_account_health(EMAIL)
    assert row["restricted_providers"] == []
    assert row["providers"]["minute"]["issue"]["code"] == "access_paused"
    assert account_health.confirmed_ban(EMAIL, row) is None
    assert row["providers"]["crowtado"]["status"] == "active"


@pytest.mark.parametrize("status,payload,expected", [
    (200, '{"userState":"active"}', "active"), (200, '{}', "invalid_response"),
    (200, '[]', "invalid_response"), (200, 'broken', "invalid_response"),
    (503, '{"userState":"active"}', "service"), (403, 'disabled', "forbidden")])
def test_quality_read_requires_valid_success(monkeypatch, status, payload, expected):
    session = minute_api.Session({"email": EMAIL})
    monkeypatch.setattr(session, "get", Mock(return_value=(status, payload)))
    if expected == "active":
        assert session.checked_quality_state("org")["userState"] == "active"
    else:
        with pytest.raises(minute_api.AuthError) as caught:
            session.checked_quality_state("org")
        assert caught.value.account_issue_code == expected


@pytest.mark.parametrize("eligibility,hold,restricted", [
    ({"checked": True, "available": True, "blocked": False}, None, False),
    ({"available": False, "blocked": False}, None, False),
    ({"available": False, "blocked": True, "reasons": ["account"]}, None, True),
    ({"available": False, "blocked": True, "reasons": ["vpn"]}, None, True),
    ({"available": False, "blocked": True, "withdrawalOverride": True}, None, False),
    ({"available": True, "blocked": False}, "hold", True)])
def test_crowtado_fresh_login_and_read_only_eligibility(monkeypatch, eligibility, hold, restricted):
    fresh = Mock()
    login = Mock(return_value=fresh)
    monkeypatch.setattr(crowtado, "login", login)
    cached = Mock(side_effect=AssertionError("Must use fresh login"))
    monkeypatch.setattr(crowtado, "_cached_login", cached)
    monkeypatch.setattr(crowtado, "_read_balance_summary", Mock(return_value={"holdReason": hold}))
    trpc = Mock(return_value=eligibility)
    monkeypatch.setattr(crowtado, "_site_trpc", trpc)
    result = crowtado.verificar_restricoes(EMAIL, "fixture-password")
    assert result["restricted"] == restricted
    login.assert_called_once_with(EMAIL, "fixture-password")
    if hold:
        trpc.assert_not_called()
    else:
        trpc.assert_called_once_with(fresh, "externalMobileCapture.eligibilityStatus", None, method="GET")
    cached.assert_not_called()


@pytest.mark.parametrize("eligibility", [{}, {"available": True, "blocked": "false"}, {"available": True, "blocked": False, "checked": False}])
def test_crowtado_login_alone_is_not_payout_clearance(monkeypatch, eligibility):
    monkeypatch.setattr(crowtado, "login", Mock())
    monkeypatch.setattr(crowtado, "_read_balance_summary", Mock(return_value={}))
    monkeypatch.setattr(crowtado, "_site_trpc", Mock(return_value=eligibility))
    with pytest.raises(crowtado.CrowtadoError) as caught:
        crowtado.verificar_restricoes(EMAIL, "fixture-password")
    assert caught.value.account_issue_code == "invalid_response"
    assert caught.value.crowtado_login_verified is True


@pytest.mark.parametrize("damage", ["owner", "boolean", "source", "services", "contradiction"])
def test_nested_health_corruption_is_not_assigned_to_an_account(local, damage):
    row = server._check_account_health(EMAIL)
    check = row["providers"]["crowtado"]
    if damage == "owner":
        check["email"] = "other@example.invalid"
    elif damage == "boolean":
        check["issue"] = {"restriction_confirmed": "true"}
    elif damage == "source":
        check["issue"] = {"provider": "minute"}
    elif damage == "services":
        del row["providers"]["minute"]
    else:
        check["issue"] = {"restriction_confirmed": True}
    server.ACCOUNT_HEALTH_PATH.write_text(json.dumps({EMAIL: row}), encoding="utf-8")
    with pytest.raises(ValueError):
        server._load_account_health_history()


def test_wallet_restriction_preserves_both_service_names(local):
    local[0].ensure_auth.side_effect = minute_api.AuthError("restricted", code="restricted")
    local[1].side_effect = crowtado.CrowtadoError("restricted", code="restricted")
    row = server._check_account_health(EMAIL)
    state = wallet.restriction({}, wallet.reading({}), {"last_check": row})
    assert "Minute" in state["reason"] and "Crowtado" in state["reason"]


def test_single_and_batch_preserve_all_provider_issues(local, monkeypatch):
    local[0].ensure_auth.side_effect = minute_api.AuthError("restricted", code="restricted")
    local[1].side_effect = crowtado.CrowtadoError("restricted", code="restricted")
    monkeypatch.setattr(server, "_list_accounts", lambda: [{"email": EMAIL}])
    monkeypatch.setattr(server, "RUNNER", Mock(running=False))
    monkeypatch.setattr(server, "RECOVERY", Mock(running=False))
    client = server.create_app(for_testing=True).test_client()
    single = client.post(f"/api/accounts/{EMAIL}/check")
    batch = client.post("/api/accounts/check-all")
    assert single.status_code == 400 and batch.status_code == 200
    assert single.json["issues"] == batch.json["results"][0]["issues"]
    assert len(single.json["issues"]) == 2 and batch.json["active"] == 0


@pytest.mark.parametrize("minute,site", [("unbanned", "login_ban"), ("unbanned", "payout_ban"),
                                       ("banned", "active"), ("unbanned", "unknown")])
def test_archived_monitor_verifies_both_services_without_restoring_access(local, monkeypatch, minute, site):
    from moneymin.web import banned_monitor
    monkeypatch.setattr(banned_monitor, "check_status", Mock(return_value={"status": minute}))
    login = Mock(return_value=Mock())
    if site == "login_ban":
        login.side_effect = crowtado.CrowtadoError("banned", code="restricted")
    monkeypatch.setattr(crowtado, "login", login)
    summary = {"availableCents": 3000, "pendingCents": 1, "inTransitCents": 0, "lifetimeCents": 3001}
    def trpc(session, proc, payload, *, method):
        assert method == "GET"
        if proc == "payouts.summary":
            return summary
        assert proc == "externalMobileCapture.eligibilityStatus"
        if site == "unknown":
            raise TimeoutError("fixture-private-token timeout")
        return {"available": site != "payout_ban", "blocked": site == "payout_ban", "reasons": ["account"]}
    monkeypatch.setattr(crowtado, "_site_trpc", trpc)
    restore = Mock(side_effect=AssertionError("Must not restore credentials"))
    monkeypatch.setattr(minute_api, "save_json", restore)
    row = banned_monitor.inspect_account({"email": EMAIL, "password": "fixture-password"})
    assert row["providers"]["minute"]["status"] == ("disabled" if minute == "banned" else "active")
    assert row["providers"]["crowtado"]["status"] == ("disabled" if site in ("login_ban", "payout_ban") else "inconclusive" if site == "unknown" else "active")
    assert row["status"] == ("inconclusive" if site == "unknown" else "banned")
    if site != "login_ban":
        assert row["balance"] == summary and not row["balance_stale"]
    assert "fixture-private-token" not in json.dumps(row)
    eligible, _ = server._banned_withdraw_eligibility({"email": EMAIL, "password": "fixture-password", "monitor": row})
    assert eligible is (site == "active")
    restore.assert_not_called()
