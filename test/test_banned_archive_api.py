import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from moneymin.web import server


class ProtectedArchiveApiTests(unittest.TestCase):
    def test_archived_password_round_trip_is_protected_and_responses_are_not_cached(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(server.config, "DATA_DIR", Path(folder)), \
             patch.object(server, "_crowtado_creds", return_value={}):
            path = Path(folder) / "banned_accounts.json"
            doc = {"schema": 1, "accounts": [{"email": "fixture@example.invalid",
                   "password": "fixture-archive-secret", "banned_at": "2026-10-03T00:00:00Z"}]}
            server.banned_store.save(path, doc)
            self.assertNotIn(b"fixture-archive-secret", path.read_bytes())
            client = server.create_app(for_testing=True).test_client()
            response = client.get("/api/accounts/banned")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.get_json()["accounts"][0]["password"], "fixture-archive-secret")
            response = client.post("/api/accounts/banned/password", json={"email": "fixture@example.invalid"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["password"], "fixture-archive-secret")
            self.assertNotIn(b"fixture-archive-secret", path.read_bytes())

    def test_corrupt_archive_is_preserved_and_returns_actionable_json(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(server.config, "DATA_DIR", Path(folder)):
            path = Path(folder) / "banned_accounts.json"
            original = b"unreadable-private-archive-fixture"
            path.write_bytes(original)
            client = server.create_app(for_testing=True).test_client()
            for route in ("/api/accounts/banned", "/api/accounts/banned/monitor"):
                with self.subTest(route=route):
                    response = client.get(route)
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.get_json()["code"], "archived_accounts_unreadable")
                    self.assertNotIn("unreadable-private", response.get_data(as_text=True))
            self.assertEqual(path.read_bytes(), original)

    def _withdraw_fixture(self, path):
        document = {"schema": 1, "accounts": [{
            "email": "fixture@example.invalid", "password": "fixture-archive-secret",
            "monitor": {"balance_status": "ok", "balance_stale": False,
                        "balance_updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "balance": {"availableCents": 3000}},
        }]}
        server.banned_store.save(path, document)

    def test_failed_archive_write_keeps_provider_withdrawal_result(self):
        outcomes = (({"ok": True, "message": "fixture request accepted",
                     "result": {"status": "ok"}}, 200),
                    ({"ok": False, "message": "fixture provider refusal",
                      "result": {"status": "refused"}}, 400))
        for outcome in outcomes:
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as folder, \
                 patch.object(server.config, "DATA_DIR", Path(folder)), \
                 patch.object(server, "BALANCES_RUNNER", Mock(running=False)), \
                 patch.object(server.BannedMonitor, "snapshot", return_value={"state": "idle"}), \
                 patch.object(server, "_withdraw_bulk_snapshot", return_value={"state": "idle"}), \
                 patch.object(server.crowtado, "consultar_saldo_api", return_value={"availableCents": 3000}), \
                 patch.object(server, "_withdraw_once", return_value=outcome) as withdraw:
                path = Path(folder) / "banned_accounts.json"
                self._withdraw_fixture(path)
                before = path.read_bytes()
                client = server.create_app(for_testing=True).test_client()
                with patch.object(server.banned_store, "save", side_effect=server.banned_store.BannedStoreError(
                        "fixture protected store unavailable")):
                    response = client.post("/api/accounts/banned/withdraw", json={"email": "fixture@example.invalid"})
                self.assertEqual(response.status_code, outcome[1])
                self.assertEqual(response.get_json(), outcome[0])
                withdraw.assert_called_once_with("fixture@example.invalid", "fixture-archive-secret")
                self.assertEqual(path.read_bytes(), before)

    def test_failed_archive_write_keeps_balance_refusal_without_withdrawal(self):
        for balance in ({"availableCents": 0}, OSError("fixture balance unavailable")):
            with self.subTest(balance=balance), tempfile.TemporaryDirectory() as folder, \
                 patch.object(server.config, "DATA_DIR", Path(folder)), \
                 patch.object(server, "BALANCES_RUNNER", Mock(running=False)), \
                 patch.object(server.BannedMonitor, "snapshot", return_value={"state": "idle"}), \
                 patch.object(server, "_withdraw_bulk_snapshot", return_value={"state": "idle"}), \
                 patch.object(server, "_withdraw_once") as withdraw:
                path = Path(folder) / "banned_accounts.json"
                self._withdraw_fixture(path)
                before = path.read_bytes()
                client = server.create_app(for_testing=True).test_client()
                with patch.object(server.crowtado, "consultar_saldo_api",
                                  side_effect=balance if isinstance(balance, Exception) else None,
                                  return_value=balance), \
                     patch.object(server.banned_store, "save", side_effect=server.banned_store.BannedStoreError(
                         "fixture protected store unavailable")):
                    response = client.post("/api/accounts/banned/withdraw", json={"email": "fixture@example.invalid"})
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.get_json())
                self.assertNotIn("archived_accounts_unreadable", str(response.get_json()))
                withdraw.assert_not_called()
                self.assertEqual(path.read_bytes(), before)
