import unittest
from unittest.mock import patch

from moneymin import crowtado
from moneymin.web import server


class PayoutFlowSafetyTests(unittest.TestCase):
    def test_paypal_without_manual_destination_can_request_once(self):
        with patch.object(crowtado, "_cached_login", return_value=object()), \
             patch.object(crowtado, "_site_trpc", side_effect=[
                 {"payoutPreference": "paypal", "manualDestinations": []},
                 {"status": "ok", "rail": "tremendous"}]) as api:
            result = crowtado.solicitar_link_saque("test@example.com", "fixture", expected_method="paypal")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(api.call_args.args[1:], ("payouts.withdraw", {"method": "paypal"}))
        self.assertEqual(api.call_count, 2)

    def test_paypal_configuration_failure_never_requests_withdrawal(self):
        with patch.object(crowtado, "configurar_metodo_saque", side_effect=TimeoutError()), \
             patch.object(crowtado, "solicitar_link_saque") as withdraw:
            result = server._withdraw_paypal_flow("test@example.com", "fixture")
        self.assertEqual(result["status"], "not_requested")
        withdraw.assert_not_called()
        self.assertIn("PayPal", server._withdraw_message("test@example.com", result))

    def test_paypal_lost_response_is_unknown_and_not_retried(self):
        with patch.object(crowtado, "configurar_metodo_saque"), \
             patch.object(crowtado, "solicitar_link_saque", side_effect=TimeoutError()) as withdraw:
            result = server._withdraw_paypal_flow("test@example.com", "fixture")
        self.assertEqual(result["status"], "unknown")
        withdraw.assert_called_once()

    def test_paypal_requires_explicit_confirmation_and_rejects_manual_destination(self):
        self.assertEqual(server._wise_withdraw_options({"method": "paypal", "paypal_confirmed": True}),
                         {"method": "paypal"})
        for body in ({"method": "paypal"}, {"method": "paypal", "paypal_confirmed": "true"},
                     {"method": "paypal", "paypal_confirmed": True, "destination_email": "other@example.com"}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                server._wise_withdraw_options(body)

    def test_delayed_wise_link_repeats_reads_not_mutations(self):
        linked = {"payoutPreference": "wise", "wiseReady": True,
                  "manualDestinations": [{"method": "wise", "isPreferred": True}]}
        with patch.object(crowtado, "_cached_login", return_value=object()), \
             patch.object(crowtado.time, "sleep"), \
             patch.object(crowtado, "_site_trpc", side_effect=[
                 ["wise", "other"], {}, {}, {"payoutPreference": "other"}, linked]) as api:
            crowtado.configurar_metodo_saque("test@example.com", "fixture", "wise", "Test Name", "recipient@example.com")
        names = [c.args[1] for c in api.call_args_list]
        self.assertEqual(names.count("kyc.saveManualPayoutMethod"), 1)
        self.assertEqual(names.count("payouts.savePayoutPreference"), 1)
        self.assertEqual(names.count("payouts.summary"), 2)
        self.assertNotIn("payouts.withdraw", names)

    def test_business_error_is_classified_without_echoing_secrets(self):
        body = [{"error": {"json": {"message": "Wise recipient@example.com already linked to another user SECRET"}}}]
        error = crowtado._trpc_error("kyc.saveManualPayoutMethod", 409, body)
        self.assertEqual(error.account_issue_code, "destination_in_use")
        self.assertNotIn("SECRET", str(error))
        self.assertNotIn("recipient@example.com", str(error))

    def test_remote_pending_retry_warns_against_duplicate_payment(self):
        for status in ("manual_pending_retry", "tremendous_pending_retry", "dots_pending_retry"):
            message = server._withdraw_message("test@example.com", {"status": status})
            self.assertIn("Não solicite outro saque", message)
            self.assertNotIn("saque não solicitado", message)

    def test_wise_cleanup_is_attempted_even_when_submission_raises(self):
        with patch.object(crowtado, "_request_withdrawal", side_effect=TimeoutError()) as request, \
             patch.object(crowtado, "finalizar_wise", return_value={
                 "wiseDestinationRemoved": True, "payoutPreferenceRestored": True}) as cleanup:
            with self.assertRaises(TimeoutError):
                crowtado.solicitar_link_saque("test@example.com", "fixture", expected_method="wise")
        request.assert_called_once()
        cleanup.assert_called_once()

    def test_preflight_failure_is_distinct_from_a_lost_withdraw_response(self):
        for summary, attempted in (({"payoutPreference": "other"}, False),
                                   ({"payoutPreference": "paypal"}, True)):
            with self.subTest(attempted=attempted), \
                 patch.object(crowtado, "_cached_login", return_value=object()), \
                 patch.object(crowtado, "_site_trpc", side_effect=[summary, TimeoutError()]) as api:
                with self.assertRaises(Exception) as caught:
                    crowtado.solicitar_link_saque("test@example.com", "fixture", expected_method="paypal")
                self.assertEqual(caught.exception.withdrawal_attempted, attempted)
                self.assertEqual(api.call_count, 2 if attempted else 1)


if __name__ == "__main__":
    unittest.main()
