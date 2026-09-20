from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from moneymin import credential_store
from moneymin.web import server


class AccountRemovalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qmoney-account-removal-")
        root = Path(self.temporary.name)
        self.data = root / "data"
        self.secrets = root / "secrets"
        self.patchers = [
            mock.patch.object(server.config, "DATA_DIR", self.data),
            mock.patch.object(server.config, "SECRETS_DIR", self.secrets),
            mock.patch.object(server, "PREFS_PATH", self.data / "webui_prefs.json"),
            mock.patch.object(server, "BALANCES_PATH", self.data / "balances.json"),
            mock.patch.object(server, "CROWTADO_PW_PATH", self.secrets / "crowtado_passwords.json"),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.client = server.create_app().test_client()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary.cleanup()

    def _write_token(self, email: str) -> Path:
        path = server.config.token_path(email)
        path.write_text(json.dumps({"email": email, "access_token": "test"}), encoding="utf-8")
        return path

    def test_removed_account_stays_hidden_if_legacy_token_returns(self):
        email = "Conta@Example.com"
        token = self._write_token(email)

        response = self.client.delete(f"/api/accounts/{email}")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(token.exists())
        self.assertEqual(server._list_accounts(), [])

        # Simula exatamente a antiga migração do desktop no próximo boot.
        self._write_token(email)
        self.assertEqual(server._list_accounts(), [])
        state = json.loads((self.data / "removed_accounts.json").read_text(encoding="utf-8"))
        self.assertEqual(state["emails"], [email.casefold()])

    def test_removal_deletes_individual_and_legacy_passwords(self):
        email = "conta@example.com"
        self._write_token(email)
        server._save_crowtado_cred(email, "fixture-password")
        self.assertEqual(credential_store.lookup(self.secrets, email), "fixture-password")

        response = self.client.delete(f"/api/accounts/{email}")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(credential_store.lookup(self.secrets, email))
        self.assertNotIn(email, server._crowtado_creds())

    def test_creation_result_can_remove_a_credential_even_without_minute_token(self):
        email = "partial@example.com"
        server._save_crowtado_cred(email, "fixture-password")
        state = {
            "state": "done",
            "results": [{"email": email, "removable": True, "removed": False}],
        }
        with mock.patch.object(server, "_BULK_REGISTER_STATE", state):
            response = self.client.delete(f"/api/accounts/{email}")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(credential_store.lookup(self.secrets, email))
        self.assertTrue(state["results"][0]["removed"])
        self.assertFalse(state["results"][0]["removable"])
        self.assertIn(email, server._removed_accounts())

    def test_manual_reconnection_reactivates_removed_account(self):
        email = "conta@example.com"
        self._write_token(email)
        self.assertEqual(self.client.delete(f"/api/accounts/{email}").status_code, 200)

        # O login real recria o token; fazemos o mesmo sem acessar a rede.
        def successful_login(login_email: str, _password: str):
            self._write_token(login_email)

        with mock.patch.object(server, "login", side_effect=successful_login), \
             mock.patch.object(server, "_resolve_org", return_value=server.config.ORG_KEY), \
             mock.patch.object(server, "_save_crowtado_cred"):
            response = self.client.post("/api/accounts", json={
                "email": email,
                "password": "test-password",
            })

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["email"] for item in server._list_accounts()], [email])
        self.assertEqual(server._removed_accounts(), set())

    def test_removal_does_not_overwrite_concurrent_preference_save(self):
        email = "remove@example.com"
        self._write_token(email)
        server._save_prefs({"selected_accounts": [email], "theme": "old"})
        read_by_removal = threading.Event()
        preference_saved = threading.Event()
        original_load = server._load_prefs

        def load():
            prefs = original_load()
            if threading.current_thread().name.startswith("removal"):
                read_by_removal.set()
                preference_saved.wait(0.5)
            return prefs

        def remove():
            with server.create_app().test_client() as client:
                return client.delete(f"/api/accounts/{email}").status_code

        def update():
            self.assertTrue(read_by_removal.wait(3))
            with server.create_app().test_client() as client:
                response = client.put("/api/preferences", json={"theme": "new"})
            preference_saved.set()
            return response.status_code

        with mock.patch.object(server, "_load_prefs", side_effect=load), \
             ThreadPoolExecutor(1, thread_name_prefix="removal") as removals, \
             ThreadPoolExecutor(1, thread_name_prefix="preferences") as preferences:
            deletion = removals.submit(remove)
            change = preferences.submit(update)
            self.assertEqual(deletion.result(timeout=5), 200)
            self.assertEqual(change.result(timeout=5), 200)
        self.assertEqual(server._load_prefs(), {"selected_accounts": [], "theme": "new"})


if __name__ == "__main__":
    unittest.main()
