from __future__ import annotations

import unittest
import urllib.error
from unittest import mock

from moneymin import crowtado
from moneymin.web import server


class CrowtadoCredentialTests(unittest.TestCase):
    def setUp(self):
        self.client = server.create_app().test_client()

    def test_connecting_existing_identity_also_keeps_balance_password(self):
        with mock.patch.object(server, "login"), \
             mock.patch.object(server, "_save_crowtado_cred") as save:
            response = self.client.post("/api/accounts", json={
                "email": "conta@example.com",
                "password": "senha-segura",
            })

        self.assertEqual(response.status_code, 200)
        save.assert_called_once_with("conta@example.com", "senha-segura")

    def test_balance_access_is_validated_on_crowtado_before_saving(self):
        account = {"email": "conta@example.com"}
        with mock.patch.object(server, "_list_accounts", return_value=[account]), \
             mock.patch.object(server.crowtado, "login") as crowtado_login, \
             mock.patch.object(server, "_save_crowtado_cred") as save:
            response = self.client.put("/api/balances/credentials", json={
                "email": "conta@example.com",
                "password": "senha-crowtado",
            })

        self.assertEqual(response.status_code, 200)
        crowtado_login.assert_called_once_with("conta@example.com", "senha-crowtado")
        save.assert_called_once_with("conta@example.com", "senha-crowtado")

    def test_unknown_identity_is_not_saved_as_crowtado_account(self):
        with mock.patch.object(server, "_list_accounts", return_value=[]), \
             mock.patch.object(server, "_save_crowtado_cred") as save:
            response = self.client.put("/api/balances/credentials", json={
                "email": "desconhecida@example.com",
                "password": "senha-crowtado",
            })

        self.assertEqual(response.status_code, 404)
        save.assert_not_called()

    def test_certificate_failure_is_translated_to_repair_action(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate")
        session = crowtado.CrowtadoSession(opener, "")

        with self.assertRaisesRegex(crowtado.CrowtadoError, "Reparar instalação"):
            session._fapi("/v1/client", {})


if __name__ == "__main__":
    unittest.main()
