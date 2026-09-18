import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch, PropertyMock

from moneymin import account_transfer as transfer
from moneymin.atomic_io import save_json, load_json
from moneymin.web import server


class AccountTransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.passwords = self.root / "passwords.json"
        self.removed = self.root / "removed.json"
        p = patch.object(transfer.config, "SECRETS_DIR", self.root)
        p.start()
        self.addCleanup(p.stop)

    def account(self, email="test@example.com"):
        return {"email": email, "password": "fixture-password",
                "token": {"email": email, "idToken": "fixture-id", "refreshToken": "fixture-refresh", "expires_at": 0}}

    def run_import(self, records, apply=True):
        return transfer.import_accounts(json.dumps(records), apply=apply,
            passwords_path=self.passwords, removed_path=self.removed)

    def test_preview_does_not_write_or_authenticate(self):
        with patch.object(transfer.minute_api, "login", side_effect=AssertionError("network")):
            result = self.run_import([self.account()], False)
        self.assertEqual(result["counts"]["new"], 1)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_round_trip_and_reimport_are_idempotent(self):
        self.run_import([self.account()])
        exported = transfer.export_accounts(None, load_json(self.passwords, {}), set())
        self.assertEqual(exported["format"], "qmoney-accounts")
        self.assertEqual(exported["accounts"][0]["password"], "fixture-password")
        result = self.run_import(exported)
        self.assertEqual(result["counts"]["duplicate"], 1)
        self.assertEqual(len(transfer.token_accounts()), 1)

    def test_case_whitespace_and_repeated_rows_preserve_existing_credentials(self):
        self.run_import([self.account()])
        changed = self.account(" TEST@EXAMPLE.COM ")
        changed["token"]["idToken"] = "different"
        result = self.run_import([changed, self.account()])
        self.assertEqual(result["counts"]["duplicate"], 2)
        self.assertEqual(next(iter(transfer.token_accounts().values()))[1]["idToken"], "fixture-id")

    def test_file_name_collision_does_not_overwrite_other_account(self):
        result = self.run_import([self.account("a.b@example.com"), self.account("a_b@example.com")])
        self.assertEqual(result["counts"]["imported"], 1)
        self.assertEqual(result["counts"]["invalid"], 1)

    def test_mixed_invalid_records_are_reported_without_secrets(self):
        bad = self.account(); bad["token"]["email"] = "other@example.com"
        result = self.run_import([bad, {"email": "../../escape", "password": "secret"}, self.account()])
        self.assertEqual(result["counts"]["invalid"], 2)
        self.assertEqual(result["counts"]["imported"], 1)
        self.assertNotIn("fixture-password", json.dumps(result))

    def test_oversized_expiry_is_invalid_and_does_not_abort_other_rows(self):
        bad = self.account("bad@example.com")
        bad["token"]["expires_at"] = 10 ** 1000
        result = self.run_import([bad, self.account()])
        self.assertEqual(result["counts"]["invalid"], 1)
        self.assertEqual(result["counts"]["imported"], 1)

    def test_password_only_login_failure_rolls_back_and_continues(self):
        def failed(email, password):
            save_json(transfer.config.token_path(email), {"email": email})
            raise RuntimeError("contains private credential")
        with patch.object(transfer.minute_api, "login", side_effect=failed):
            result = self.run_import([{"email": "bad@example.com", "password": "secret"}, self.account()])
        self.assertEqual(result["counts"]["error"], 1)
        self.assertEqual(result["counts"]["imported"], 1)
        self.assertFalse(transfer.config.token_path("bad@example.com").exists())
        self.assertNotIn("private credential", json.dumps(result))

    def test_save_failure_restores_all_files(self):
        save_json(self.passwords, {"kept@example.com": "kept"})
        original = self.passwords.read_bytes()
        real = transfer.save_json
        def fail(path, data):
            if path == self.removed:
                raise OSError("disk full")
            real(path, data)
        with patch.object(transfer, "save_json", side_effect=fail):
            result = self.run_import([self.account()])
        self.assertEqual(result["counts"]["error"], 1)
        self.assertEqual(self.passwords.read_bytes(), original)
        self.assertEqual(transfer.token_accounts(), {})

    def test_removed_account_can_be_restored_explicitly(self):
        save_json(self.removed, {"emails": ["test@example.com"]})
        self.run_import([self.account()])
        self.assertEqual(load_json(self.removed, {})["emails"], [])

    def test_failed_rollback_still_restores_other_files_and_stops_batch(self):
        email = "test@example.com"
        token_path = transfer.config.token_path(email)
        save_json(token_path, self.account()["token"])
        save_json(self.passwords, {email: "original password"})
        save_json(self.removed, {"emails": [email]})
        original_passwords = self.passwords.read_bytes()
        original_removed = self.removed.read_bytes()
        real_save = transfer.save_json
        real_restore = transfer.save_bytes

        def save(path, data):
            if path == self.removed:
                raise OSError("disk failure")
            real_save(path, data)

        def restore(path, data):
            if path == token_path:
                raise OSError("private path")
            real_restore(path, data)

        with patch.object(transfer, "save_json", side_effect=save), \
             patch.object(transfer, "save_bytes", side_effect=restore):
            result = self.run_import([self.account(), self.account("next@example.com")])
        self.assertEqual(result["counts"]["error"], 2)
        self.assertEqual(self.passwords.read_bytes(), original_passwords)
        self.assertEqual(self.removed.read_bytes(), original_removed)
        self.assertFalse(transfer.config.token_path("next@example.com").exists())
        self.assertNotIn("private path", json.dumps(result))

    def test_concurrent_imports_create_one_account(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.run_import([self.account()]), range(2)))
        self.assertEqual(sum(r["counts"]["imported"] for r in results), 1)
        self.assertEqual(sum(r["counts"]["duplicate"] for r in results), 1)

    def test_strict_schema_bom_limits_and_duplicate_json_keys(self):
        self.assertEqual(len(transfer.decode_document('\ufeff' + json.dumps([self.account()]))), 1)
        for raw in ('[]', '{"email":"a","email":"b"}', '{"format":"qmoney-accounts","version":2,"accounts":[]}', 'null', '[', ' ' * (transfer.MAX_BYTES + 1)):
            with self.subTest(raw=raw[:40]), self.assertRaises(ValueError):
                transfer.decode_document(raw)

    def test_export_selection_deduplicates_and_excludes_removed(self):
        self.run_import([self.account(), self.account("other@example.com")])
        result = transfer.export_accounts(["test@example.com", "TEST@example.com"], {}, set())
        self.assertEqual(len(result["accounts"]), 1)
        result = transfer.export_accounts(None, {}, {"test@example.com"})
        self.assertEqual(result["accounts"][0]["email"], "other@example.com")

    def test_http_preview_import_export_and_busy_guard(self):
        with patch.object(server.config, "DATA_DIR", self.root), \
             patch.object(server, "CROWTADO_PW_PATH", self.passwords), \
             patch.object(server, "PREFS_PATH", self.root / "prefs.json"), \
             patch.object(server.RUNNER.__class__, "running", new_callable=PropertyMock, return_value=False), \
             patch.object(server.BALANCES_RUNNER.__class__, "running", new_callable=PropertyMock, return_value=False):
            client = server.create_app().test_client()
            content = json.dumps([self.account()])
            self.assertEqual(client.post('/api/accounts/import', json={'content': content}).json['counts']['new'], 1)
            result = client.post('/api/accounts/import', json={'content': content, 'apply': True})
            self.assertEqual(result.json['counts']['imported'], 1)
            exported = client.post('/api/accounts/export', json={})
            self.assertEqual(exported.headers['Cache-Control'], 'no-store')
            self.assertEqual(len(exported.json['accounts']), 1)
            self.assertEqual(client.post('/api/accounts/export', json={'emails': []}).status_code, 400)
            self.assertEqual(client.post('/api/accounts/import', json={'content': content, 'apply': 'yes'}).status_code, 400)

    def test_http_running_campaign_blocks_import(self):
        with patch.object(server.RUNNER.__class__, "running", new_callable=PropertyMock, return_value=True):
            response = server.create_app().test_client().post('/api/accounts/import',
                json={'content': json.dumps([self.account()]), 'apply': True})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(transfer.token_accounts(), {})

    def test_legacy_token_field_names(self):
        account = {"email": "test@example.com", "id_token": "id", "refresh_token": "refresh"}
        self.assertEqual(self.run_import([account])["counts"]["imported"], 1)

    def test_corrupt_local_password_store_is_preserved(self):
        self.passwords.write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.run_import([self.account()])
        self.assertEqual(self.passwords.read_text(), '{broken')
        self.assertEqual(transfer.token_accounts(), {})

    def test_password_only_success_and_duplicate_in_same_file(self):
        def login(email, password):
            self.assertEqual(password, ' keep spaces ')
            save_json(transfer.config.token_path(email), self.account(email)['token'])
        with patch.object(transfer.minute_api, 'login', side_effect=login) as authenticate:
            result = self.run_import([{'email': 'NEW@example.com', 'senha': ' keep spaces '},
                                      {'email': 'new@example.com', 'password': 'other'}])
        self.assertEqual(result['counts']['imported'], 1)
        self.assertEqual(result['counts']['duplicate'], 1)
        authenticate.assert_called_once()
        self.assertEqual(load_json(self.passwords, {})['new@example.com'], ' keep spaces ')


if __name__ == "__main__":
    unittest.main()
