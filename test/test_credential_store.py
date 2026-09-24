import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from moneymin import account_transfer, credential_store, minute_api
from moneymin.web import server


class CredentialStoreTests(unittest.TestCase):
    def test_active_account_password_is_revealed_only_on_request(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            email = "owner@example.invalid"
            credential_store.save(root, email, "current-password")
            with patch.object(server.config, "SECRETS_DIR", root), \
                 patch.object(server, "CROWTADO_PW_PATH", root / "legacy.json"), \
                 patch.object(server, "_list_accounts", return_value=[{"email": email}]), \
                 patch.object(server, "RUNNER", Mock()), \
                 patch.object(server, "ORG_MIGRATION", Mock(running=False)):
                client = server.create_app().test_client()
                listing = client.get("/api/accounts").get_json()["accounts"][0]
                self.assertTrue(listing["has_password"])
                self.assertNotIn("current-password", json.dumps(listing))
                response = client.post("/api/accounts/password", json={"email": email.upper()})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["password"], "current-password")
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(client.post("/api/accounts/password", json={
                    "email": "other@example.invalid"}).status_code, 404)

                credential_store.record_path(root, email).write_bytes(b"{corrupt")
                (root / "legacy.json").write_text(json.dumps({email: "old-password"}))
                self.assertFalse(client.get("/api/accounts").get_json()["accounts"][0]["has_password"])
                self.assertEqual(client.post("/api/accounts/password", json={
                    "email": email}).status_code, 404)

    def test_registration_checkpoints_password_before_any_remote_signup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            email = "owner@example.invalid"
            password = "fixture-password-only"
            identity = {"email": email, "senha": password, "birth_month": 1,
                        "birth_year": 1990, "gender": "male"}
            session = Mock()
            session.ensure_auth.return_value = {"organizations": [{
                "resourceKey": server.config.ORG_KEY, "disabled": False,
            }]}

            def signup(saved_email, saved_password):
                self.assertEqual((saved_email, saved_password), (email, password))
                self.assertEqual(credential_store.lookup(root, email), password)

            def register(saved_email, saved_password, invite_code):
                self.assertEqual(invite_code, server.config.INVITE_CODE)
                account_transfer.save_json(account_transfer.config.token_path(saved_email), {
                    "email": saved_email, "idToken": "fixture-id",
                    "refreshToken": "fixture-refresh", "expires_at": 0,
                })

            with patch.object(server.config, "SECRETS_DIR", root), \
                 patch.object(account_transfer.config, "SECRETS_DIR", root), \
                 patch.object(server, "CROWTADO_PW_PATH", root / "crowtado_passwords.json"), \
                 patch.object(server.account_bans, "require_not_banned"), \
                 patch.object(server, "_set_account_removed"), \
                 patch.object(server, "_cache_org_key"), \
                 patch.object(server.crowtado, "criar_conta", side_effect=signup), \
                 patch.object(server.crowtado, "preencher_demografia"), \
                 patch.object(server.crowtado, "vincular_minute"), \
                 patch.object(server.crowtado, "login"), \
                 patch.object(minute_api, "register", side_effect=register), \
                 patch.object(minute_api, "login"), \
                 patch.object(server.Session, "from_email", return_value=session):
                result = server._full_register_account(email, password, identity)
                exported = account_transfer.export_accounts([email], server._crowtado_creds(), set())
            self.assertIsNone(result["error"])
            self.assertEqual(result["steps"]["save_partial"]["status"], "ok")
            self.assertEqual(exported["accounts"][0]["password"], password)

    def test_saved_credential_is_read_back_and_exported_with_its_token(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            email = "owner@example.invalid"
            password = "fixture-password-only"
            with patch.object(server.config, "SECRETS_DIR", root), \
                 patch.object(account_transfer.config, "SECRETS_DIR", root), \
                 patch.object(server, "CROWTADO_PW_PATH", root / "crowtado_passwords.json"):
                server._save_crowtado_cred(email, password)
                self.assertEqual(credential_store.lookup(root, email), password)
                self.assertEqual(server._crowtado_creds()[email], password)
                account_transfer.save_json(account_transfer.config.token_path(email), {
                    "email": email, "idToken": "fixture-id", "refreshToken": "fixture-refresh",
                    "expires_at": 0,
                })
                exported = account_transfer.export_accounts([email], server._crowtado_creds(), set())
            round_trip = json.loads(json.dumps(exported))
            self.assertEqual(round_trip["accounts"][0]["email"], email)
            self.assertEqual(round_trip["accounts"][0]["password"], password)

    def test_corrupt_legacy_mapping_cannot_block_new_individual_credential(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            legacy = root / "crowtado_passwords.json"
            legacy.write_bytes(b"{corrupt")
            with patch.object(server.config, "SECRETS_DIR", root), \
                 patch.object(server, "CROWTADO_PW_PATH", legacy):
                server._save_crowtado_cred("owner@example.invalid", "fixture-password-only")
                self.assertEqual(server._crowtado_creds()["owner@example.invalid"], "fixture-password-only")
            self.assertEqual(legacy.read_bytes(), b"{corrupt")

    def test_corrupt_individual_record_is_preserved_and_blocks_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            email = "owner@example.invalid"
            credential_store.save(root, email, "first-password")
            stored = next((root / credential_store.DIRECTORY).glob("*.json"))
            stored.write_bytes(b"{corrupt")
            with self.assertRaises(ValueError):
                credential_store.save(root, email, "second-password")
            self.assertEqual(stored.read_bytes(), b"{corrupt")

    def test_strict_lookup_blocks_legacy_fallback_on_corrupt_record(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            email = "owner@example.invalid"
            credential_store.save(root, email, "current-password")
            credential_store.record_path(root, email).write_bytes(b"{corrupt")
            self.assertIsNone(credential_store.lookup(root, email))
            with self.assertRaises(ValueError):
                credential_store.lookup(root, email, strict=True)

    def test_delete_removes_only_requested_account(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            credential_store.save(root, "first@example.invalid", "first-password")
            credential_store.save(root, "second@example.invalid", "second-password")
            credential_store.delete(root, "first@example.invalid")
            self.assertIsNone(credential_store.lookup(root, "first@example.invalid"))
            self.assertEqual(
                credential_store.lookup(root, "second@example.invalid"),
                "second-password",
            )

    def test_renamed_record_cannot_claim_another_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            email = "owner@example.invalid"
            credential_store.save(root, email, "fixture-password-only")
            original = credential_store.record_path(root, email)
            original.rename(original.with_name("0" * 64 + ".json"))
            self.assertEqual(credential_store.load_all(root), {})

    def test_export_refuses_a_crowtado_backup_without_a_password(self):
        account = {"email": "owner@example.invalid"}
        with patch.object(server, "_list_accounts", return_value=[account]), \
             patch.object(server, "_crowtado_creds", return_value={}):
            response = server.create_app().test_client().post(
                "/api/accounts/export", json={"emails": [account["email"]]})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["accounts"], [account["email"]])

    def test_export_refuses_corrupt_primary_credential_even_with_legacy_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            email = "owner@example.invalid"
            credential_store.save(root, email, "current-password")
            credential_store.record_path(root, email).write_bytes(b"{corrupt")
            legacy = root / "crowtado_passwords.json"
            legacy.write_text(json.dumps({email: "old-password"}), encoding="utf-8")
            with patch.object(server.config, "SECRETS_DIR", root), \
                 patch.object(account_transfer.config, "SECRETS_DIR", root), \
                 patch.object(server, "CROWTADO_PW_PATH", legacy), \
                 patch.object(server, "_list_accounts", return_value=[{"email": email}]):
                account_transfer.save_json(account_transfer.config.token_path(email), {
                    "email": email, "idToken": "fixture-id",
                    "refreshToken": "fixture-refresh", "expires_at": 0,
                })
                response = server.create_app().test_client().post(
                    "/api/accounts/export", json={"emails": [email]})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.get_json()["accounts"], [email])
            self.assertNotIn("old-password", response.get_data(as_text=True))

    def test_invalid_identity_or_oversized_password_is_never_written(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for email, password in (("not-an-email", "password"),
                                    ("owner@example.invalid", "x" * 4097)):
                with self.subTest(email=email, password_length=len(password)), \
                     self.assertRaises(ValueError):
                    credential_store.save(root, email, password)
            self.assertFalse((root / credential_store.DIRECTORY).exists())

    def test_corrupt_primary_credential_blocks_automatic_legacy_relogin(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            email = "owner@example.invalid"
            credential_store.save(root, email, "current-password")
            credential_store.record_path(root, email).write_bytes(b"{corrupt")
            (root / "crowtado_passwords.json").write_text(
                json.dumps({email: "old-password"}), encoding="utf-8")
            with patch.object(minute_api.config, "SECRETS_DIR", root), \
                 patch.object(minute_api.config, "DATA_DIR", root):
                self.assertIsNone(minute_api._lookup_password(email))
