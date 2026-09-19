import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moneymin import secure_store
from moneymin.web import server


class StoragePreservationTests(unittest.TestCase):
    def test_local_api_rejects_other_instance_token_and_anonymous_requests(self):
        with patch.dict(os.environ, {"QMONEY_LOCAL_API_TOKEN": "instance-a"}):
            app = server.create_app()
        @app.get("/api/isolation-fixture")
        def fixture():
            return {"ok": True}
        client = app.test_client()
        for path in ("/api/isolation-fixture", "/api/accounts", "/api/integrations"):
            for token in (None, "instance-b"):
                with self.subTest(path=path, token=token):
                    response = client.get(path, headers={"X-QMoney-Session": token} if token else {})
                    self.assertEqual(response.status_code, 401)
        self.assertEqual(client.get("/api/isolation-fixture", headers={"X-QMoney-Session": "instance-a"}).status_code, 200)

    def test_packaged_service_requires_session_token(self):
        with patch.dict(os.environ, {"QMONEY_LOCAL_API_TOKEN": ""}), \
             patch.object(server.sys, "frozen", True, create=True):
            with self.assertRaises(RuntimeError):
                server.create_app()

    def test_unreadable_vault_returns_actionable_json(self):
        with patch.object(server, "_migrate_legacy_integrations",
                          side_effect=secure_store.SecureStoreError("Cofre preservado.")):
            response = server.create_app().test_client().get("/api/integrations")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "local_vault_unreadable")

    def test_login_success_with_local_save_failure_is_partial(self):
        with patch.object(server, "login"), \
             patch.object(server, "_save_crowtado_cred", side_effect=ValueError("bad file")), \
             patch.object(server, "_set_account_removed") as restore:
            response = server.create_app().test_client().post("/api/accounts", json={
                "email": "fixture@example.invalid", "password": "test-only"})
        self.assertEqual(response.status_code, 500)
        self.assertTrue(response.get_json()["partial"])
        self.assertFalse(response.get_json()["ok"])
        restore.assert_not_called()

    def test_account_list_survives_corrupt_files_without_modifying_them(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            files = {"token_bad_utf8.json": b'\xff',
                     "token_bad_email.json": b'{"email": ["invalid"]}',
                     "token_good.json": b'{"email": "valid@example.invalid"}'}
            for name, content in files.items():
                (root / name).write_bytes(content)
            with patch.object(server.config, "tokens_dir", return_value=root), \
                 patch.object(server, "_load_prefs", return_value={"org_keys": []}), \
                 patch.object(server, "_removed_accounts", return_value=set()), \
                 patch.object(server, "ACCOUNT_HEALTH_PATH", root / "health.json"):
                accounts = server._list_accounts()
            self.assertEqual([a["email"] for a in accounts], ["valid@example.invalid"])
            for name, content in files.items():
                self.assertEqual((root / name).read_bytes(), content)

    def test_invalid_password_file_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "passwords.json"
            for content in (b'{', b'[]', b'null', b'{"user@example.invalid": 123}', b'\xff'):
                with self.subTest(content=content), patch.object(server, "CROWTADO_PW_PATH", path):
                    path.write_bytes(content)
                    with self.assertRaises(ValueError):
                        server._save_crowtado_cred("fixture@example.invalid", "test-only")
                    self.assertEqual(path.read_bytes(), content)

    def test_unreadable_vault_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "integrations.dat"
            original = b"encrypted-by-another-user-fixture"
            path.write_bytes(original)
            with patch.object(secure_store, "_crypt", side_effect=OSError("cannot decrypt")):
                self.assertEqual(secure_store.load_secure_settings(path), {})
                for operation in (
                    lambda: secure_store.save_secure_settings(path, {"schema": 1}),
                    lambda: secure_store.update_secure_section(path, "hostinger", {"token": "fixture"}),
                ):
                    with self.assertRaises(ValueError):
                        operation()
                    self.assertEqual(path.read_bytes(), original)

    def test_non_object_vault_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "integrations.dat"
            path.write_bytes(b"original")
            with patch.object(secure_store, "_crypt", return_value=b"[]"):
                with self.assertRaises(ValueError):
                    secure_store.save_secure_settings(path, {})
            self.assertEqual(path.read_bytes(), b"original")

    @unittest.skipUnless(os.name == "nt", "DPAPI requires Windows")
    def test_real_dpapi_roundtrip_in_directory_with_spaces_and_unicode(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "perfil de teste ç" / "integrations.dat"
            secure_store.save_secure_settings(path, {"schema": 1, "ego4d": {"token": "test-only"}})
            self.assertNotIn(b"test-only", path.read_bytes())
            secure_store.update_secure_section(path, "hostinger", {"token": "other-test-only"})
            data = secure_store.load_secure_settings(path, strict=True)
            self.assertEqual(data["ego4d"]["token"], "test-only")
            self.assertEqual(data["hostinger"]["token"], "other-test-only")
