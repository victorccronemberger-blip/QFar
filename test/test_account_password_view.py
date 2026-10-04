"""Explicit credential viewing uses only configured local identities and stores."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from moneymin.web import server


class AccountPasswordViewTests(unittest.TestCase):
    EMAIL = "fixture@example.invalid"
    PASSWORD = " fixture-Password:áß<>! "
    TOKEN = "fixture-local-api-session-token"

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.data = self.root / "data"
        self.secrets = self.root / "secrets"
        self.data.mkdir()
        self.secrets.mkdir()
        self.legacy = self.data / "crowtado_passwords.json"
        self.archive = self.data / "banned_accounts.json"
        self.stack.enter_context(patch.dict(os.environ, {"QMONEY_LOCAL_API_TOKEN": self.TOKEN}))
        self.stack.enter_context(patch.object(server.config, "DATA_DIR", self.data))
        self.stack.enter_context(patch.object(server.config, "SECRETS_DIR", self.secrets))
        self.stack.enter_context(patch.object(server, "CROWTADO_PW_PATH", self.legacy))
        self.accounts = self.stack.enter_context(patch.object(
            server, "_list_accounts", return_value=[{"email": self.EMAIL}]))
        self.client = server.create_app().test_client()

    def post(self, route: str, email: str | None = None):
        return self.client.post(route, json={"email": email or self.EMAIL},
                                headers={"X-QMoney-Session": self.TOKEN})

    def save_password(self, password: str | None = None, email: str | None = None):
        server.credential_store.save(self.secrets, email or self.EMAIL,
                                     self.PASSWORD if password is None else password)

    def save_archive(self, rows):
        server.banned_store.save(self.archive, {"schema": 1, "accounts": rows})

    def assert_credential_response(self, response, password: str | None = None):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            "email": self.EMAIL, "password": self.PASSWORD if password is None else password,
        })
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_active_password_is_exact_and_email_is_normalized(self):
        self.save_password()
        self.assert_credential_response(self.post(
            "/api/accounts/password", "  Fixture@Example.Invalid  "))
        payload = server.credential_store.record_path(self.secrets, self.EMAIL).read_bytes()
        self.assertNotIn(self.PASSWORD.encode("utf-8"), payload)

    def test_active_current_protected_password_wins_over_legacy_copy(self):
        self.legacy.write_text(json.dumps({self.EMAIL: "fixture-obsolete-password"}), encoding="utf-8")
        before = self.legacy.read_bytes()
        self.save_password()
        self.assert_credential_response(self.post("/api/accounts/password"))
        self.assertEqual(self.legacy.read_bytes(), before)

    def test_active_corrupt_protected_record_blocks_legacy_fallback(self):
        self.save_password()
        path = server.credential_store.record_path(self.secrets, self.EMAIL)
        corrupted = b"fixture-unreadable-private-record"
        path.write_bytes(corrupted)
        self.legacy.write_text(json.dumps({self.EMAIL: "fixture-obsolete-password"}), encoding="utf-8")
        response = self.post("/api/accounts/password")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("fixture-obsolete", response.get_data(as_text=True))
        self.assertNotIn("fixture-unreadable", response.get_data(as_text=True))
        self.assertEqual(path.read_bytes(), corrupted)

    def test_active_without_saved_password_returns_404(self):
        response = self.post("/api/accounts/password")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("password", response.get_json())

    def test_active_endpoint_does_not_reveal_an_unconfigured_identity(self):
        foreign = "other-fixture@example.invalid"
        self.save_password(email=foreign)
        with patch.object(server, "_saved_account_password") as read:
            response = self.post("/api/accounts/password", foreign)
        self.assertEqual(response.status_code, 404)
        read.assert_not_called()
        self.assertNotIn("password", response.get_json())

    def test_archived_password_is_exact_and_email_is_normalized(self):
        self.save_archive([{"email": self.EMAIL, "password": self.PASSWORD}])
        self.assert_credential_response(self.post(
            "/api/accounts/banned/password", " Fixture@Example.Invalid "))
        self.assertNotIn(self.PASSWORD.encode("utf-8"), self.archive.read_bytes())

    def test_archived_password_remains_available_after_active_record_is_removed(self):
        self.accounts.return_value = []
        self.save_archive([{"email": self.EMAIL, "password": self.PASSWORD}])
        self.assert_credential_response(self.post("/api/accounts/banned/password"))
        self.assertEqual(self.post("/api/accounts/password").status_code, 404)

    def test_archived_without_snapshot_uses_current_protected_password(self):
        self.save_archive([{"email": self.EMAIL}])
        self.save_password()
        self.legacy.write_text(json.dumps({self.EMAIL: "fixture-obsolete-password"}), encoding="utf-8")
        self.assert_credential_response(self.post("/api/accounts/banned/password"))

    def test_archived_corrupt_individual_record_blocks_legacy_fallback(self):
        self.save_archive([{"email": self.EMAIL}])
        self.save_password()
        path = server.credential_store.record_path(self.secrets, self.EMAIL)
        corrupted = b"fixture-unreadable-private-record"
        path.write_bytes(corrupted)
        self.legacy.write_text(json.dumps({self.EMAIL: "fixture-obsolete-password"}), encoding="utf-8")
        response = self.client.get("/api/accounts/banned",
                                   headers={"X-QMoney-Session": self.TOKEN})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIsNone(response.get_json()["accounts"][0].get("password"))
        self.assertNotIn("fixture-obsolete", response.get_data(as_text=True))
        self.assertIsNone(server.banned_store.load(self.archive)["accounts"][0].get("password"))
        monitor = self.client.get("/api/accounts/banned/monitor",
                                  headers={"X-QMoney-Session": self.TOKEN})
        self.assertEqual(monitor.status_code, 200)
        self.assertFalse(monitor.get_json()["accounts"][0]["has_password"])
        response = self.post("/api/accounts/banned/password")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("fixture-obsolete", response.get_data(as_text=True))
        self.assertNotIn("fixture-unreadable", response.get_data(as_text=True))
        self.assertEqual(path.read_bytes(), corrupted)

    def test_archived_snapshot_is_authoritative_if_active_record_is_corrupt(self):
        self.save_archive([{"email": self.EMAIL, "password": self.PASSWORD}])
        self.save_password(password="fixture-new-active-password")
        path = server.credential_store.record_path(self.secrets, self.EMAIL)
        path.write_bytes(b"fixture-unreadable-private-record")
        self.assert_credential_response(self.post("/api/accounts/banned/password"))

    def test_archived_without_saved_password_returns_404(self):
        self.save_archive([{"email": self.EMAIL}])
        response = self.post("/api/accounts/banned/password")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("password", response.get_json())

    def test_archived_endpoint_does_not_reveal_a_non_archived_identity(self):
        self.save_password()
        self.save_archive([{"email": "other-fixture@example.invalid", "password": "fixture-other"}])
        with patch.object(server, "_crowtado_creds") as read:
            response = self.post("/api/accounts/banned/password")
        self.assertEqual(response.status_code, 404)
        read.assert_not_called()
        self.assertNotIn("password", response.get_json())

    def test_corrupt_archive_is_preserved_without_exposing_record_payload(self):
        original = b"fixture-private-corrupt-archive"
        self.archive.write_bytes(original)
        with patch.object(server, "_crowtado_creds") as read:
            response = self.post("/api/accounts/banned/password")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "archived_accounts_unreadable")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("fixture-private", response.get_data(as_text=True))
        self.assertEqual(self.archive.read_bytes(), original)
        read.assert_not_called()

    def test_both_password_endpoints_require_the_local_session_token(self):
        self.save_password()
        self.save_archive([{"email": self.EMAIL, "password": self.PASSWORD}])
        for route in ("/api/accounts/password", "/api/accounts/banned/password"):
            for token in (None, "fixture-wrong-token"):
                with self.subTest(route=route, token=token), \
                     patch.object(server, "_crowtado_creds") as read, \
                     patch.object(server.banned_store, "load") as load:
                    response = self.client.post(
                        route, json={"email": self.EMAIL},
                        headers={} if token is None else {"X-QMoney-Session": token})
                    self.assertEqual(response.status_code, 401)
                    self.assertNotIn("password", response.get_json())
                    self.assertNotIn(self.PASSWORD, response.get_data(as_text=True))
                    read.assert_not_called()
                    load.assert_not_called()
