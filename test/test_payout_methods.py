from __future__ import annotations

import unittest
from unittest.mock import patch

from moneymin import crowtado
from moneymin.web import server


class PayoutMethodTests(unittest.TestCase):
    def test_paypal_saves_destination_then_preference(self):
        calls = []

        def trpc(_session, procedure, payload, method="POST"):
            calls.append((procedure, payload, method))
            if procedure == "payouts.payoutMethods":
                return ["other", "paypal", "wise"]
            if procedure == "payouts.summary":
                return {"payoutPreference": "paypal", "manualDestinations": [
                    {"method": "paypal", "isPreferred": True}]}
            return {"ok": True}

        with patch.object(crowtado, "_cached_login", return_value=object()), \
             patch.object(crowtado, "_site_trpc", side_effect=trpc):
            crowtado.configurar_metodo_saque("account@example.com", "pw", "paypal",
                                             "Person Name", "person@example.com")
        self.assertEqual(calls[1][0], "kyc.saveManualPayoutMethod")
        self.assertEqual(calls[1][1]["paypalEmail"], "person@example.com")
        self.assertEqual(calls[1][1]["makePreferred"], True)
        self.assertEqual(calls[2][1], {"method": "paypal"})

    def test_dots_maps_to_other_without_manual_destination(self):
        calls = []

        def trpc(_session, procedure, payload, method="POST"):
            calls.append((procedure, payload))
            if procedure == "payouts.payoutMethods":
                return ["other", "paypal"]
            if procedure == "payouts.summary":
                return {"payoutPreference": "other"}
            return {}

        with patch.object(crowtado, "_cached_login", return_value=object()), \
             patch.object(crowtado, "_site_trpc", side_effect=trpc):
            crowtado.configurar_metodo_saque("account@example.com", "pw", "dots")
        self.assertEqual([call[0] for call in calls],
                         ["payouts.payoutMethods", "payouts.savePayoutPreference", "payouts.summary"])
        self.assertEqual(calls[1][1], {"method": "other"})

    def test_withdraw_uses_remote_preference(self):
        calls = []

        def trpc(_session, procedure, payload, method="POST"):
            calls.append((procedure, payload))
            if procedure == "payouts.summary":
                return {"payoutPreference": "wise", "wiseReady": True,
                        "manualDestinations": [{"method": "wise", "isPreferred": True}]}
            return {"status": "ok", "rail": "wise"}

        with patch.object(crowtado, "_cached_login", return_value=object()), \
             patch.object(crowtado, "_site_trpc", side_effect=trpc):
            result = crowtado.solicitar_link_saque("account@example.com", "pw")
        self.assertEqual(calls[1], ("payouts.withdraw", {"method": "wise"}))
        self.assertEqual(result["rail"], "wise")

    def test_batch_report_does_not_contain_destination(self):
        with patch.object(server.crowtado, "configurar_metodo_saque",
                          side_effect=ValueError("person@example.com rejected")):
            with server._PAYOUT_METHOD_LOCK:
                server._PAYOUT_METHOD_STATE.update(state="running", total=1, done=0, results=[])
            server._payout_method_run({"account@example.com": "pw"}, "paypal",
                                      "Person Name", "person@example.com")
        snapshot = server._payout_method_snapshot()
        self.assertEqual(snapshot["state"], "done")
        self.assertNotIn("person@example.com", str(snapshot))

    def test_apply_all_targets_only_connected_crowtado_accounts(self):
        class ImmediateThread:
            def __init__(self, *, target, args, **_kwargs):
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)

        with server._PAYOUT_METHOD_LOCK:
            server._PAYOUT_METHOD_STATE.update(state="idle", total=0, done=0, results=[])
        accounts = [{"email": "crow@example.com"}, {"email": "claru@example.com"}]
        with patch.object(server, "_list_accounts", return_value=accounts), \
             patch.object(server.org_policy, "account_kind",
                          side_effect=lambda email: "claru" if email.startswith("claru") else "crowtado"), \
             patch.object(server, "_configured_crowtado_creds", return_value={
                 "crow@example.com": "pw", "claru@example.com": "pw", "orphan@example.com": "pw"}), \
             patch.object(server.threading, "Thread", ImmediateThread), \
             patch.object(server.crowtado, "configurar_metodo_saque") as configure:
            response = server.create_app().test_client().post(
                "/api/balances/payout-methods/apply-all", json={"method": "dots"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["total"], 1)
        configure.assert_called_once_with("crow@example.com", "pw", "dots", "", "")

    def test_duplicate_wise_destination_stops_batch(self):
        with server._PAYOUT_METHOD_LOCK:
            server._PAYOUT_METHOD_STATE.update(state="running", total=3, done=0, results=[])
        with patch.object(server.crowtado, "configurar_metodo_saque",
                          side_effect=crowtado.CrowtadoError(
                              "That Wise account is already linked to another user")) as configure:
            server._payout_method_run({"one@example.com": "a", "two@example.com": "b",
                                       "three@example.com": "c"}, "wise", "Person Name",
                                      "person@example.com")
        snapshot = server._payout_method_snapshot()
        self.assertEqual(snapshot["state"], "blocked")
        self.assertEqual(snapshot["done"], 1)
        configure.assert_called_once()

    def test_withdraw_fails_closed_without_preference(self):
        with patch.object(crowtado, "_cached_login", return_value=object()), \
             patch.object(crowtado, "_site_trpc", return_value={}) as trpc:
            with self.assertRaises(crowtado.CrowtadoError):
                crowtado.solicitar_link_saque("account@example.com", "pw")
        trpc.assert_called_once()


if __name__ == "__main__":
    unittest.main()
