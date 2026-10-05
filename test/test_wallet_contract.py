"""Wallet monetary readings, restrictions and read-only recovery contracts."""
from datetime import datetime, timezone
import json
from unittest.mock import patch

import pytest

from moneymin import crowtado
from moneymin.web import runner, server, wallet


NOW = 1_800_000_000.0
ELIGIBLE = {"checked": True, "available": True, "blocked": False, "reasons": []}


def balance(**changes):
    return {"availableCents": 4500, "pendingCents": 2300, "inTransitCents": 0,
            "lifetimeCents": 6800, "updated_at": datetime.fromtimestamp(NOW-60, timezone.utc).isoformat(),
            "contributorEligibility": dict(ELIGIBLE), **changes}


@pytest.mark.parametrize("invalid", [None, True, -1, 1.5, float("nan"), float("inf"), "3500", 2**53])
@pytest.mark.parametrize("field", wallet.MONEY_FIELDS)
def test_invalid_readings_never_confirm_or_authorize(invalid, field):
    record = balance(**{field: invalid})
    state = wallet.reading(record, now=NOW)
    assert not state["confirmed"]
    assert not wallet.payout(record, state, True)["eligible"]


@pytest.mark.parametrize("stamp,code", [(None,"invalid"),("wrong","invalid"),
    (datetime.fromtimestamp(NOW, timezone.utc).replace(tzinfo=None).isoformat(),"invalid"),
    (datetime.fromtimestamp(NOW+1, timezone.utc).isoformat(),"expired"),
    (datetime.fromtimestamp(NOW-wallet.MAX_AGE_S, timezone.utc).isoformat(),"expired")])
def test_invalid_future_naive_or_expired_dates_are_not_current(stamp, code):
    state = wallet.reading(balance(updated_at=stamp), now=NOW)
    assert state["code"] == code
    assert not state["confirmed"]


def test_zero_is_confirmed_but_not_eligible_and_missing_is_unknown():
    record = balance(availableCents=0, pendingCents=0, lifetimeCents=0)
    state = wallet.reading(record, now=NOW)
    assert state["confirmed"]
    assert wallet.payout(record,state,True)["code"] == "minimum"
    assert wallet.reading({}, now=NOW)["code"] == "unqueried"


def test_restricted_login_can_have_money_but_never_an_eligible_payout():
    record = balance(banned=False, locked=False,
        holdReason="Conta desativada — fale com o suporte.",
        contributorEligibility={**ELIGIBLE,"blocked":True,"reasons":["account"]})
    data=wallet.snapshot([{"email":"restricted"}], {"restricted":record}, {"restricted"}, {}, {}, now=NOW)
    row=data["accounts"]["restricted"]
    assert row["reading"]["confirmed"]
    assert row["restriction"]["code"] == "disabled"
    assert "desativada" in row["restriction"]["label"]
    assert not row["payout"]["eligible"]
    assert data["totals"]["availableCents"] == 4500
    assert data["withdrawable_total_cents"] == 0


def test_failed_read_does_not_erase_previous_hold_or_claim_account_cleared():
    record=balance(error="Falha de conexão", stale=True, holdReason="Conta desativada — fale com o suporte.")
    data=wallet.snapshot([{"email":"restricted"}], {"restricted":record}, {"restricted"}, {}, {}, now=NOW)
    row=data["accounts"]["restricted"]
    assert not row["reading"]["confirmed"]
    assert row["restriction"]["code"] == "historical_hold"
    assert "desativada" in row["restriction"]["reason"]
    assert data["totals"]["availableCents"] == 0
    assert data["historical_totals"]["availableCents"] == 4500


def test_hold_money_alone_does_not_imply_account_disabled_or_block_available_money():
    record=balance(onHoldCents=1000)
    data=wallet.snapshot([{"email":"regular"}], {"regular":record}, {"regular"}, {}, {}, now=NOW)
    assert data["accounts"]["regular"]["restriction"]["code"] == "clear"
    assert data["accounts"]["regular"]["payout"]["eligible"]


@pytest.mark.parametrize("eligibility", [None, {}, {"checked":False},
    {"checked":True,"available":False,"blocked":False}, {**ELIGIBLE,"blocked":True}])
def test_unknown_or_blocked_eligibility_preserves_money_and_blocks_submission(eligibility):
    record=balance(contributorEligibility=eligibility)
    state=wallet.reading(record, now=NOW)
    assert state["confirmed"]
    assert not wallet.payout(record,state,True)["eligible"]


def test_override_only_clears_contributor_hold_and_not_a_separate_payout_hold():
    record=balance(contributorEligibility={**ELIGIBLE,"blocked":True,"withdrawalOverride":True})
    assert wallet.payout(record,wallet.reading(record,now=NOW),True)["eligible"]
    record["holdReason"]="Retenção de saque"
    assert not wallet.payout(record,wallet.reading(record,now=NOW),True)["eligible"]


def test_totals_exclude_expired_failed_claru_and_orphan_balances_and_report_optional_coverage():
    records={"ready":balance(),"old":balance(updated_at="2020-01-01T00:00:00Z"),
             "failed":balance(stale=True),"claru":balance(),"orphan":balance()}
    accounts=[{"email":e} for e in ("ready","old","failed","claru","new")]
    data=wallet.snapshot(accounts,records,set(records),{"claru":"claru"},{},now=NOW)
    assert data["counts"]["confirmed"] == 1
    assert data["counts"]["eligible"] == 1
    assert data["totals"]["availableCents"] == 4500
    assert data["historical_totals"]["availableCents"] == 9000
    assert data["field_coverage"]["onHoldCents"] == 0
    assert "orphan" not in data["accounts"]


def test_withdrawable_total_excludes_restricted_and_below_minimum_accounts():
    records={"regular":balance(), "restricted":balance(holdReason="Conta desativada"),
             "minimum":balance(availableCents=2500)}
    data=wallet.snapshot([{"email":email} for email in records],records,set(records),{}, {},now=NOW)
    assert data["withdrawable_total_cents"] == 4500
    assert data["totals"]["availableCents"] == 11500
    assert data["counts"]["eligible"] == 1
    assert data["counts"]["attention"] == 1


def test_unsafe_aggregate_is_unavailable_instead_of_rounded():
    data=wallet.snapshot([{"email":"a"},{"email":"b"}],
        {"a":balance(availableCents=2**53-1),"b":balance()},set(),{}, {},now=NOW)
    assert data["totals"]["availableCents"] is None


def test_rsc_parser_handles_nested_destinations_and_does_not_retain_private_data():
    raw={**balance(),"manualDestinations":[{"email":"private@example.com","nested":{"secret":"TOKEN"}}],
         "holdReason":"Mensagem com \"aspas\""}
    parsed=crowtado._extrai_summary(json.dumps(raw))
    assert parsed["availableCents"] == 4500
    assert parsed["holdReason"] == raw["holdReason"]
    assert "private@example.com" not in repr(parsed)
    assert "TOKEN" not in repr(parsed)


def test_eligibility_normalization_excludes_private_fields_and_unrecognized_reasons():
    raw={"available":True,"blocked":True,"reasons":["account","vpn","private@example.com"],
         "deviceNames":["PRIVATE"],"token":"SECRET"}
    result=crowtado._normalize_eligibility([{"result":{"data":{"json":raw}}}])
    assert result["reasons"] == ["account","vpn"]
    assert "PRIVATE" not in repr(result) and "SECRET" not in repr(result)
    assert crowtado._normalize_eligibility({"available":"true","blocked":False}) == {"checked":False}


def test_transient_summary_get_retries_same_session_once_and_preserves_restriction():
    hold={**balance(),"holdReason":"Conta desativada — fale com o suporte."}
    with patch.object(crowtado,"_cached_login",return_value="same") as login, \
         patch.object(crowtado.time,"sleep"), \
         patch.object(crowtado,"_site_trpc",side_effect=[crowtado.CrowtadoError("timeout",code="timeout"),
             hold,{"available":True,"blocked":True,"reasons":["account"]}]) as query:
        record=crowtado.consultar_saldo_api("fixture@example.com","pw")
    login.assert_called_once()
    assert all(call.args[0] == "same" and call.kwargs["method"] == "GET" for call in query.call_args_list)
    assert record["holdReason"] == hold["holdReason"]
    assert record["contributorEligibility"]["blocked"]


def test_read_failure_does_not_relogin_or_launch_browser_for_a_confirmed_restriction():
    error=crowtado.CrowtadoError("Restrição",code="restricted")
    assert not crowtado.can_use_browser_fallback(error)
    with patch.object(crowtado,"_cached_login",return_value="same") as login, \
         patch.object(crowtado,"_site_trpc",side_effect=error) as query:
        with pytest.raises(crowtado.CrowtadoError): crowtado.consultar_saldo_api("a","pw")
    assert login.call_count == query.call_count == 1


@pytest.mark.parametrize("summary,eligibility", [({"payoutPreference":"paypal"},ELIGIBLE),
    ({**balance(),"payoutPreference":"paypal","holdReason":"Conta desativada"},ELIGIBLE),
    ({**balance(),"payoutPreference":"paypal"},{"available":True,"blocked":True,"reasons":["account"]}),
    ({**balance(),"payoutPreference":"paypal"},{})])
def test_live_preflight_never_posts_with_incomplete_or_restricted_account(summary, eligibility):
    def query(session,procedure,*args,**kwargs):
        assert kwargs["method"] == "GET"
        return summary if procedure == "payouts.summary" else eligibility
    with patch.object(crowtado,"_cached_login",return_value="same"), \
         patch.object(crowtado,"_site_trpc",side_effect=query):
        if "availableCents" not in summary:
            with pytest.raises(crowtado.CrowtadoError) as caught:
                crowtado.solicitar_link_saque("a","pw",expected_method="paypal")
            assert caught.value.withdrawal_attempted is False
        else:
            result = crowtado.solicitar_link_saque("a","pw",expected_method="paypal")
            assert result["withdrawalAttempted"] is False
            assert result["status"] in {"on_hold", "eligibility_unconfirmed"}


def test_stop_finishes_current_read_and_save_before_skipping_the_next_account():
    instance=runner.BalancesRunner()
    instance.state="running"
    instance.total=2
    instance.results={e:{"email":e,"state":"queued"} for e in ("a","b")}
    events=[]
    def query(email,password):
        events.append(("read",email))
        assert instance.snapshot()["current_email"] == email
        instance.stop()
        return balance()
    with patch.object(crowtado,"consultar_saldo_api",side_effect=query):
        instance._run({"a":"pw","b":"pw"},lambda e,*args: events.append(("save",e)))
    assert events == [("read","a"),("save","a")]
    state=instance.snapshot()
    assert state["state"] == "stopped" and state["done"] == 1
    assert state["results"][1]["state"] == "not_consulted"


def test_wallet_commands_cannot_race_before_worker_state_is_published():
    client=server.create_app(for_testing=True).test_client()
    with server._WALLET_COMMAND_LOCK, patch.object(server.BALANCES_RUNNER,"start") as start:
        response=client.post("/api/balances/refresh",json={})
    assert response.status_code == 409
    start.assert_not_called()


def test_wallet_endpoint_exposes_only_configured_balances_and_consistent_read_model():
    client=server.create_app(for_testing=True).test_client()
    recent=balance()
    with patch.object(wallet.time,"time",return_value=NOW), \
         patch.object(server,"_list_accounts",return_value=[{"email":"configured"}]), \
         patch.object(server,"_load_balances",return_value={"configured":recent,"removed":balance()}), \
         patch.object(server,"_configured_crowtado_creds",return_value={"configured":"PRIVATE"}), \
         patch.object(server,"_crowtado_creds",return_value={}), \
         patch.object(server,"_saved_account_password",return_value=None), \
         patch.object(server.org_policy,"account_kind",return_value="crowtado"), \
         patch.object(server.fx,"usd_brl_quote",return_value={}):
        result=client.get("/api/balances").get_json()
    assert set(result["balances"]) == {"configured"}
    assert result["wallet"]["accounts"]["configured"]["reading"]["confirmed"], result["wallet"]["accounts"]["configured"]["reading"]
    assert result["wallet"]["accounts"]["configured"]["payout"]["eligible"]
    assert result["refresh_needed"] == []
    assert "PRIVATE" not in repr(result)


@pytest.mark.parametrize("status", ["hold", "on_hold"])
def test_withdrawal_hold_reason_is_visible_and_preserved_instead_of_only_login_flags(tmp_path, status):
    result={"status":status,"holdReason":"Conta desativada — fale com o suporte."}
    with patch.object(server,"BALANCES_PATH",tmp_path/"balances.json"):
        server._save_balances({"a":balance()})
        server._invalidate_balance_after_withdrawal("a",result)
        saved=server._load_balances()["a"]
    assert "Conta desativada" in server._withdraw_message("a",result)
    assert saved["availableCents"] == 4500
    assert saved["holdReason"] == result["holdReason"]
    assert saved["stale"]
    assert wallet.restriction(saved,wallet.reading(saved,now=NOW),{})["code"] == "historical_hold"
