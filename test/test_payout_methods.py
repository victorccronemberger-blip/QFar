from __future__ import annotations

import unittest
from unittest.mock import patch

from moneymin import crowtado
from moneymin.web import server


class PayoutMethodTests(unittest.TestCase):
    def test_cleanup_is_idempotent_and_checks_final_remote_state(self):
        for initially_linked in (True, False):
            with self.subTest(initially_linked=initially_linked):
                state = {"linked": initially_linked, "preference": "wise"}
                calls = []
                def trpc(session, procedure, payload, method="POST"):
                    calls.append(procedure)
                    if procedure == "payouts.summary":
                        return {"wiseReady": state["linked"], "payoutPreference": state["preference"],
                                "manualDestinations": [{"method": "wise"}] if state["linked"] else []}
                    if procedure == "kyc.removeManualPayoutMethod":
                        state["linked"] = False
                        # Resposta perdida depois da remoção real: consulta final deve resolver.
                        raise TimeoutError()
                    if procedure == "payouts.payoutMethods":
                        return ["wise", "other"]
                    if procedure == "payouts.savePayoutPreference":
                        state["preference"] = payload["method"]
                        return {}
                    self.fail(procedure)
                with patch.object(crowtado, "_cached_login", return_value=object()), \
                     patch.object(crowtado, "_site_trpc", side_effect=trpc):
                    for _ in range(2):
                        self.assertEqual(crowtado.finalizar_wise("one@example.com", "pw"), {
                            "wiseDestinationRemoved": True, "payoutPreferenceRestored": True})
                self.assertEqual(calls.count("kyc.removeManualPayoutMethod"), int(initially_linked))
                self.assertNotIn("payouts.withdraw", calls)

    def test_saved_wise_cannot_withdraw_without_explicit_confirmation(self):
        with patch.object(crowtado, "_cached_login", return_value=object()), \
             patch.object(crowtado, "_site_trpc", return_value={"payoutPreference": "wise"}) as trpc:
            with self.assertRaisesRegex(crowtado.CrowtadoError, "confirme Wise"):
                crowtado.solicitar_link_saque("account@example.com", "pw")
        trpc.assert_called_once()

    def test_confirmed_wise_never_withdraws_through_another_method(self):
        with patch.object(crowtado, "_cached_login", return_value=object()), \
             patch.object(crowtado, "_site_trpc", return_value={"payoutPreference": "other"}) as trpc:
            with self.assertRaises(crowtado.CrowtadoError):
                crowtado.solicitar_link_saque("account@example.com", "pw", expected_method="wise")
        trpc.assert_called_once()

    def test_wise_cannot_be_prelinked_to_every_account(self):
        with patch.object(server.crowtado, "configurar_metodo_saque") as configure:
            response = server.create_app().test_client().post(
                "/api/balances/payout-methods/apply-all", json={"method": "wise"})
        self.assertEqual(response.status_code, 400)
        configure.assert_not_called()

    def test_unlink_must_be_confirmed_in_summary(self):
        for summary in ({"wiseReady": True},
                        {"wiseReady": False, "manualDestinations": [{"method": "wise"}]}, {}, None):
            with self.subTest(summary=summary), \
                 patch.object(crowtado, "_cached_login", return_value=object()), \
                 patch.object(crowtado, "_site_trpc", side_effect=[{}, summary]):
                with self.assertRaises(crowtado.CrowtadoError):
                    crowtado.desvincular_wise("account@example.com", "pw")

    def test_unlink_rejection_preserves_accepted_withdrawal_and_restores_dots(self):
        summary = {"payoutPreference": "wise", "wiseReady": True,
                   "manualDestinations": [{"method": "wise", "isPreferred": True}]}
        with patch.object(crowtado, "_cached_login", return_value=object()), \
             patch.object(crowtado, "_site_trpc", side_effect=[summary, {"status": "ok"}]), \
             patch.object(crowtado, "desvincular_wise", side_effect=crowtado.CrowtadoError("conflict")), \
             patch.object(crowtado, "configurar_metodo_saque") as restore:
            result = crowtado.solicitar_link_saque("account@example.com", "pw", expected_method="wise")
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["wiseDestinationRemoved"])
        self.assertTrue(result["payoutPreferenceRestored"])
        restore.assert_called_once_with("account@example.com", "pw", "dots")
        self.assertIn("desvinculação", server._withdraw_message("account@example.com", result))

    def test_five_wise_accounts_complete_sequentially_without_network(self):
        accounts = [f"simulated-{index}@example.com" for index in range(1, 6)]
        states = {email: {"preference": "other", "linked": False, "withdrawals": 0}
                  for email in accounts}
        events = []

        def trpc(email, procedure, payload, method="POST"):
            state = states[email]
            events.append((email, procedure))
            if procedure == "payouts.payoutMethods":
                return ["wise", "other"]
            if procedure == "kyc.saveManualPayoutMethod":
                self.assertEqual(payload["method"], "wise")
                self.assertFalse(any(value["linked"] for value in states.values()))
                state["linked"] = True
                return {"ok": True}
            if procedure == "kyc.removeManualPayoutMethod":
                self.assertEqual(state["withdrawals"], 1)
                self.assertEqual(payload, {"method": "wise"})
                state["linked"] = False
                return {"ok": True}
            if procedure == "payouts.savePayoutPreference":
                if payload["method"] == "other":
                    self.assertEqual(state["withdrawals"], 1)
                state["preference"] = payload["method"]
                return {"ok": True}
            if procedure == "payouts.summary":
                return {"payoutPreference": state["preference"], "wiseReady": state["linked"],
                        "manualDestinations": [{"method": "wise", "isPreferred": True}]
                        if state["linked"] else []}
            if procedure == "payouts.withdraw":
                self.assertTrue(state["linked"])
                self.assertEqual(state["preference"], "wise")
                self.assertEqual(payload, {"method": "wise"})
                state["withdrawals"] += 1
                return {"status": "ok", "rail": "wise"}
            self.fail(f"Unexpected procedure: {procedure}")

        with patch("socket.socket.connect", side_effect=AssertionError("Network prohibited")), \
             patch.object(crowtado, "_cached_login", side_effect=lambda email, password: email), \
             patch.object(crowtado, "_site_trpc", side_effect=trpc):
            for index, email in enumerate(accounts):
                with self.subTest(account=email):
                    # A conta anterior deve concluir antes de iniciar a seguinte.
                    for previous in accounts[:index]:
                        self.assertEqual(states[previous]["preference"], "other")
                        self.assertEqual(states[previous]["withdrawals"], 1)
                    crowtado.configurar_metodo_saque(
                        email, "fake-password", "wise", "Test Recipient", "recipient@example.com")
                    result = crowtado.solicitar_link_saque(email, "fake-password", expected_method="wise")
                    self.assertEqual(result["status"], "ok")
                    self.assertTrue(result["payoutPreferenceRestored"])
                    self.assertTrue(result["wiseDestinationRemoved"])
                    self.assertEqual(states[email]["preference"], "other")
                    self.assertEqual(states[email]["withdrawals"], 1)

        expected_procedures = [
            "payouts.payoutMethods", "kyc.saveManualPayoutMethod",
            "payouts.savePayoutPreference", "payouts.summary", "payouts.summary",
            "payouts.withdraw", "kyc.removeManualPayoutMethod", "payouts.summary", "payouts.payoutMethods",
            "payouts.savePayoutPreference", "payouts.summary"]
        self.assertEqual(events, [(email, procedure) for email in accounts
                                  for procedure in expected_procedures])

    def test_wise_link_withdraw_restore_flow(self):
        calls = []
        preference = "other"
        linked = False

        def trpc(_session, procedure, payload, method="POST"):
            nonlocal preference, linked
            calls.append((procedure, payload))
            if procedure == "payouts.payoutMethods":
                return ["other", "wise"]
            if procedure == "payouts.savePayoutPreference":
                preference = payload["method"]
            if procedure == "kyc.saveManualPayoutMethod":
                linked = True
            if procedure == "kyc.removeManualPayoutMethod":
                linked = False
            if procedure == "payouts.summary":
                return {"payoutPreference": preference, "wiseReady": linked,
                        "manualDestinations": [{"method": "wise", "isPreferred": True}] if linked else []}
            if procedure == "payouts.withdraw":
                self.assertEqual(preference, "wise")
                return {"status": "ok", "rail": "wise", "privateToken": "secret"}
            return {}

        with patch.object(crowtado, "_cached_login", return_value=object()), \
             patch.object(crowtado, "_site_trpc", side_effect=trpc):
            crowtado.configurar_metodo_saque("test-account@example.com", "pw", "wise",
                                             "Test Recipient", "recipient@example.com")
            result = crowtado.solicitar_link_saque("test-account@example.com", "pw", expected_method="wise")
        self.assertEqual(preference, "other")
        self.assertTrue(result["payoutPreferenceRestored"])
        self.assertTrue(result["wiseDestinationRemoved"])
        self.assertNotIn("privateToken", result)
        self.assertEqual([name for name, _ in calls], [
            "payouts.payoutMethods", "kyc.saveManualPayoutMethod",
            "payouts.savePayoutPreference", "payouts.summary", "payouts.summary",
            "payouts.withdraw", "kyc.removeManualPayoutMethod", "payouts.summary", "payouts.payoutMethods",
            "payouts.savePayoutPreference", "payouts.summary"])

    def test_wise_only_restores_on_confirmed_success(self):
        summary = {"payoutPreference": "wise", "wiseReady": True,
                   "manualDestinations": [{"method": "wise", "isPreferred": True}]}
        for response in ({"status": "below_minimum"}, {"status": "review_required"},
                         {}, crowtado.CrowtadoError("timeout")):
            with self.subTest(response=response), \
                 patch.object(crowtado, "_cached_login", return_value=object()), \
                 patch.object(crowtado, "_site_trpc", side_effect=[summary, response]), \
                 patch.object(crowtado, "configurar_metodo_saque") as restore:
                if not response or isinstance(response, Exception):
                    with self.assertRaises(crowtado.CrowtadoError):
                        crowtado.solicitar_link_saque("test@example.com", "pw", expected_method="wise")
                else:
                    crowtado.solicitar_link_saque("test@example.com", "pw", expected_method="wise")
                restore.assert_not_called()

    def test_restore_failure_preserves_success_and_warns(self):
        summary = {"payoutPreference": "wise", "wiseReady": True,
                   "manualDestinations": [{"method": "wise", "isPreferred": True}]}
        with patch.object(crowtado, "_cached_login", return_value=object()), \
             patch.object(crowtado, "_site_trpc", side_effect=[summary, {"status": "ok"}]), \
             patch.object(crowtado, "configurar_metodo_saque", side_effect=RuntimeError("secret")):
            result = crowtado.solicitar_link_saque("test@example.com", "pw", expected_method="wise")
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["payoutPreferenceRestored"])
        self.assertNotIn("secret", str(result))
        self.assertIn("sem repetir o saque", server._withdraw_message("test@example.com", result))

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
            result = crowtado.solicitar_link_saque("account@example.com", "pw", expected_method="wise")
        self.assertEqual(calls[1], ("payouts.withdraw", {"method": "wise"}))
        self.assertEqual(result["rail"], "wise")
        self.assertFalse(result["payoutPreferenceRestored"])

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
                crowtado.solicitar_link_saque("account@example.com", "pw", expected_method="wise")
        trpc.assert_called_once()


if __name__ == "__main__":
    unittest.main()
