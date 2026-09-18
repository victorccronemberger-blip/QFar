from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path
import urllib.error
from unittest import mock

from moneymin import crowtado
from moneymin.web import server


class CrowtadoCredentialTests(unittest.TestCase):
    def setUp(self):
        self.client = server.create_app().test_client()

    def test_connecting_existing_identity_also_keeps_balance_password(self):
        with mock.patch.object(server, "login"), \
             mock.patch.object(server, "_set_account_removed"), \
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

    def test_balances_ignore_credentials_from_removed_identities(self):
        accounts = [{"email": "ativa@example.com"}]
        saved = {
            "ativa@example.com": "senha-atual",
            "removida@example.com": "senha-antiga",
        }
        with mock.patch.object(server, "_list_accounts", return_value=accounts), \
             mock.patch.object(server, "_crowtado_creds", return_value=saved), \
             mock.patch.object(server, "_load_balances", return_value={}), \
             mock.patch.object(server.fx, "usd_brl_quote", return_value={}):
            response = self.client.get("/api/balances")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["with_password"], ["ativa@example.com"])

    def test_certificate_failure_is_translated_to_repair_action(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate")
        session = crowtado.CrowtadoSession(opener, "")

        with self.assertRaisesRegex(crowtado.CrowtadoError, "Reparar instalação"):
            session._fapi("/v1/client", {})

    def test_creation_batch_password_is_reused_without_second_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'novas_contas_20260916.json').write_text(json.dumps([
                {'email': ' NEW@Example.com ', 'senha': ' kept spaces '},
                {'email': 'removed@example.com', 'senha': 'old'}, None]), encoding='utf-8')
            with mock.patch.object(server.config, 'DATA_DIR', root), \
                 mock.patch.object(server, 'CROWTADO_PW_PATH', root / 'passwords.json'), \
                 mock.patch.object(server, '_list_accounts', return_value=[{'email': 'New@example.com'}]):
                self.assertEqual(server._configured_crowtado_creds(), {'New@example.com': ' kept spaces '})
                with mock.patch.object(server.BALANCES_RUNNER, 'start') as start:
                    response = self.client.post('/api/balances/refresh', json={})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(start.call_args.args[0], {'New@example.com': ' kept spaces '})

    def test_invalid_legacy_line_does_not_hide_valid_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = (b'\xef\xbb\xbf{"email":"first@example.com","senha":"first"}\n'
                        b'{"email":"bad@example.com","senha":"\xff"}\n'
                        b'{"email":"last@example.com","senha":"last"}\n')
            legacy = root / "contas.jsonl"
            legacy.write_bytes(original)
            with mock.patch.object(server.config, "DATA_DIR", root), \
                 mock.patch.object(server, "CROWTADO_PW_PATH", root / "passwords.json"):
                self.assertEqual(server._crowtado_creds(), {
                    "first@example.com": "first", "last@example.com": "last"})
            self.assertEqual(legacy.read_bytes(), original)

    def test_explicit_saved_password_wins_and_case_variants_are_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); pw = root / 'passwords.json'
            (root / 'novas_contas_20260916.json').write_text(json.dumps([
                {'email': 'test@example.com', 'senha': 'outdated'}]), encoding='utf-8')
            pw.write_text(json.dumps({'TEST@example.com': 'current'}), encoding='utf-8')
            with mock.patch.object(server.config, 'DATA_DIR', root), mock.patch.object(server, 'CROWTADO_PW_PATH', pw):
                self.assertEqual(server._crowtado_creds()['test@example.com'], 'current')
                server._save_crowtado_cred(' Test@example.com ', 'replacement')
                self.assertEqual(json.loads(pw.read_text()), {'test@example.com': 'replacement'})


if __name__ == "__main__":
    unittest.main()
