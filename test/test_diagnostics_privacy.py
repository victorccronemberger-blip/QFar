import unittest
from unittest.mock import patch

from moneymin.web import server


class DiagnosticsPrivacyTests(unittest.TestCase):
    def test_readiness_exceptions_and_details_are_never_exported(self):
        secret = "SECRET_TOKEN private@example.com https://private.invalid/?sig=SECRET_TOKEN C:/private/secrets"
        for mode in ("exception", "check"):
            with self.subTest(mode=mode), \
                 patch.object(server.readiness, "campaign_readiness",
                              side_effect=RuntimeError(secret) if mode == "exception" else None,
                              return_value={"ready": False, "checks": [{"name": "Acelerador Ego4D", "status": "error", "detail": secret},
                                                                        {"name": secret, "status": secret}]}), \
                 patch.object(server, "_list_accounts", return_value=[{"email": "private@example.com"}]), \
                 patch.object(server, "_storage_snapshot", return_value={"data_files": 2}) as storage, \
                 patch.object(server.campaign, "list_campaign_logs", return_value=[]):
                response = server.create_app().test_client().get("/api/diagnostics")
                self.assertEqual(response.status_code, 200)
                body = response.get_data(as_text=True)
                for marker in ("SECRET_TOKEN", "private@example.com", "private.invalid", "C:/private"):
                    self.assertNotIn(marker, body)
                self.assertTrue(response.get_json()["readiness"]["details_omitted"])
                storage.assert_called_once_with(include_path=False)
