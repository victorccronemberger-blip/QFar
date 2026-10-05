"""Clerk account flags and fresh login validation with isolated provider replies."""
from unittest.mock import patch
import pytest
from moneymin import crowtado


@pytest.mark.parametrize("flag", ["banned", "locked"])
@pytest.mark.parametrize("where", ["response", "client"])
def test_explicit_restriction_rejects_otherwise_complete_login(flag, where):
    body = {"response":{"status":"complete", "created_session_id":"fixture-session"}}
    user = {"banned":False,"locked":False,flag:True}
    if where == "response":
        body["response"]["user"] = user
    else:
        body["client"] = {"sessions":[{"id":"fixture-session","user":user}]}
    with patch.object(crowtado.CrowtadoSession,"_fapi",side_effect=[(200,{}),(200,body)]):
        with pytest.raises(crowtado.CrowtadoError) as error:
            crowtado.login("fixture@example.invalid","fixture-password")
    assert error.value.account_issue_code == "restricted"


def test_complete_login_with_clear_flags_remains_valid():
    body = {"response":{"status":"complete","created_session_id":"fixture-session","user":{"banned":False,"locked":False}}}
    with patch.object(crowtado.CrowtadoSession,"_fapi",side_effect=[(200,{}),(200,body)]):
        assert crowtado.login("fixture@example.invalid","fixture-password").session_id == "fixture-session"


def test_other_session_flags_do_not_mark_current_user_banned():
    crowtado._check_login_user_flags({"client":{"sessions":[{"id":"other-session","user":{"banned":True}}]}},"current-session")


def test_nonboolean_flags_are_inconclusive_rather_than_success():
    with pytest.raises(crowtado.CrowtadoError) as error:
        crowtado._check_login_user_flags({"response":{"user":{"banned":"false"}}},"fixture-session")
    assert error.value.account_issue_code == "invalid_response"
