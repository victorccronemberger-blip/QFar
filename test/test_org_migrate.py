import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from moneymin import org_migrate
from moneymin.minute_api import AuthError


class OrgMigrateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.json_path = self.root / "contas.json"

    def _write(self, accounts) -> Path:
        doc = {
            "format": "qmoney-accounts",
            "version": 1,
            "accounts": accounts,
        }
        self.json_path.write_text(json.dumps(doc), encoding="utf-8")
        return self.json_path

    def _account(self, email="user@example.com"):
        return {
            "email": email,
            "password": "secret-pass",
            "token": {
                "email": email,
                "idToken": "id-token",
                "refreshToken": "refresh-token",
                "localId": "uid",
                "expiresIn": "3600",
                "expires_at": 1,
            },
        }

    def test_load_records_from_qmoney_export(self) -> None:
        self._write([self._account("a@x.com"), self._account("b@x.com")])
        records = org_migrate.load_records(self.json_path)
        self.assertEqual([item["email"] for item in records], ["a@x.com", "b@x.com"])
        self.assertEqual(records[0]["password"], "secret-pass")

    def test_already_in_target_org_does_not_join(self) -> None:
        record = org_migrate.account_transfer.clean_record(self._account())
        sess = mock.Mock()
        sess.ensure_auth.return_value = {
            "organizations": [{"name": "Datoric", "resourceKey": "NEWORG"}],
        }
        sess.data = {"idToken": "id", "refreshToken": "ref", "expires_at": 1}
        with mock.patch.object(org_migrate, "session_from_record", return_value=sess), \
             mock.patch.object(org_migrate, "_set_pref_org") as prefs:
            row = org_migrate.migrate_one(record, code="PE8EAR5V", org_key="NEWORG")
        self.assertEqual(row["status"], "already")
        sess.join_org.assert_not_called()
        prefs.assert_called_once_with("user@example.com", "NEWORG")
        self.assertNotIn("password", json.dumps(row))

    def test_join_marks_migrated(self) -> None:
        record = org_migrate.account_transfer.clean_record(self._account())
        sess = mock.Mock()
        sess.ensure_auth.return_value = {
            "organizations": [{"name": "Hub", "resourceKey": "OLD"}],
        }
        sess.join_org.return_value = (200, json.dumps({
            "organization": {"resourceKey": "NEWORG", "name": "Datoric"},
        }))
        sess.me.return_value = {
            "organizations": [
                {"name": "Hub", "resourceKey": "OLD"},
                {"name": "Datoric", "resourceKey": "NEWORG"},
            ],
        }
        sess.data = {"idToken": "id", "refreshToken": "ref", "expires_at": 1}
        with mock.patch.object(org_migrate, "session_from_record", return_value=sess), \
             mock.patch.object(org_migrate, "_set_pref_org"):
            row = org_migrate.migrate_one(record, code="PE8EAR5V", org_key="NEWORG")
        self.assertEqual(row["status"], "migrated")
        sess.join_org.assert_called_once_with("PE8EAR5V")

    def test_restricted_account_is_classified(self) -> None:
        record = org_migrate.account_transfer.clean_record(self._account())
        with mock.patch.object(
            org_migrate, "session_from_record",
            side_effect=AuthError("conta desativada no HUB: user@example.com"),
        ):
            row = org_migrate.migrate_one(record, code="PE8EAR5V", org_key="NEWORG")
        self.assertEqual(row["status"], "restricted")
        self.assertNotIn("secret-pass", json.dumps(row))

    def test_dry_run_skips_network(self) -> None:
        self._write([self._account("a@x.com")])
        with mock.patch.object(org_migrate, "session_from_record") as sess:
            report = org_migrate.migrate_file(self.json_path, dry_run=True)
        sess.assert_not_called()
        self.assertEqual(report["counts"]["dry_run"], 1)
        self.assertEqual(report["code"], org_migrate.config.INVITE_CODE)

    def test_invalid_entries_do_not_abort_file(self) -> None:
        invalid = [17, "not-an-account", ["value"], None, False, {}]
        self._write(invalid + [self._account()])
        with mock.patch.object(org_migrate, "session_from_record") as auth:
            report = org_migrate.migrate_file(self.json_path, dry_run=True)
        auth.assert_not_called()
        self.assertEqual(report["counts"], {"invalid": 6, "dry_run": 1})
        self.assertEqual(
            [row["email"] for row in report["results"][:-1]],
            [f"linha-{index}" for index in range(1, 7)],
        )

    def test_mixed_batch_uses_each_accounts_organization(self) -> None:
        config = org_migrate.config
        for claru_already_joined in (False, True):
            with self.subTest(claru_already_joined=claru_already_joined):
                self._write([
                    self._account("crow@example.com"),
                    self._account("user@supply.claru.ai"),
                ])
                crow, claru = mock.Mock(), mock.Mock()
                for sess, key in ((crow, config.ORG_KEY), (claru, config.CLARU_ORG_KEY)):
                    sess.ensure_auth.return_value = {"organizations": []}
                    sess.join_org.return_value = (200, "{}")
                    sess.me.return_value = {"organizations": [{"resourceKey": key}]}
                    sess.data = {"idToken": "private-token"}
                if claru_already_joined:
                    claru.ensure_auth.return_value = claru.me.return_value
                with mock.patch.object(org_migrate, "session_from_record", side_effect=[crow, claru]), \
                     mock.patch.object(org_migrate, "_set_pref_org") as prefs:
                    report = org_migrate.migrate_file(self.json_path, delay_s=0)
                crow.join_org.assert_called_once_with(config.INVITE_CODE)
                if claru_already_joined:
                    claru.join_org.assert_not_called()
                else:
                    claru.join_org.assert_called_once_with(config.CLARU_INVITE_CODE)
                self.assertEqual(prefs.call_args_list, [
                    mock.call("crow@example.com", config.ORG_KEY),
                    mock.call("user@supply.claru.ai", config.CLARU_ORG_KEY),
                ])
                self.assertIsNone(report["code"])
                self.assertIsNone(report["org_key"])
                self.assertEqual(report["results"][1]["org_key"], config.CLARU_ORG_KEY)
                self.assertNotIn("private-token", json.dumps(report))

    def test_claru_dry_run_reports_claru_target(self) -> None:
        self._write([self._account("user@supply.claru.ai")])
        with mock.patch.object(org_migrate, "session_from_record") as auth:
            report = org_migrate.migrate_file(self.json_path, dry_run=True)
        auth.assert_not_called()
        self.assertEqual(report["code"], org_migrate.config.CLARU_INVITE_CODE)
        self.assertEqual(report["org_key"], org_migrate.config.CLARU_ORG_KEY)

    def test_explicit_target_is_preserved(self) -> None:
        self._write([self._account("user@supply.claru.ai")])
        sess = mock.Mock()
        sess.ensure_auth.return_value = {"organizations": []}
        sess.join_org.return_value = (200, "{}")
        sess.me.return_value = {"organizations": [{"resourceKey": "CUSTOMORG"}]}
        sess.data = {}
        with mock.patch.object(org_migrate, "session_from_record", return_value=sess), \
             mock.patch.object(org_migrate, "_set_pref_org"):
            report = org_migrate.migrate_file(
                self.json_path, code="CUSTOMCODE", org_key="CUSTOMORG", delay_s=0,
            )
        sess.join_org.assert_called_once_with("CUSTOMCODE")
        self.assertEqual(report["counts"], {"migrated": 1})
        self.assertEqual(report["org_key"], "CUSTOMORG")

    def test_profile_failure_does_not_abort_remaining_accounts(self) -> None:
        self._write([self._account("a@example.com"), self._account("b@example.com")])
        first, second = mock.Mock(), mock.Mock()
        first.ensure_auth.return_value = {"organizations": []}
        first.join_org.return_value = (200, "{}")
        first.me.side_effect = RuntimeError("timeout secret-pass private-token")
        second.ensure_auth.return_value = {
            "organizations": [{"resourceKey": org_migrate.config.ORG_KEY}],
        }
        second.data = {"idToken": "private-token"}
        progress = mock.Mock()
        with mock.patch.object(org_migrate, "session_from_record", side_effect=[first, second]) as auth, \
             mock.patch.object(org_migrate, "_set_pref_org") as prefs:
            report = org_migrate.migrate_file(self.json_path, delay_s=0, on_progress=progress)
        self.assertEqual(auth.call_count, 2)
        self.assertEqual(progress.call_count, 2)
        prefs.assert_called_once_with("b@example.com", org_migrate.config.ORG_KEY)
        self.assertEqual(report["counts"], {"error": 1, "already": 1})
        self.assertEqual(report["results"][0]["detail"], "RuntimeError")
        self.assertNotIn("secret-pass", json.dumps(report))
        self.assertNotIn("private-token", json.dumps(report))

    def test_preference_failure_does_not_abort_remaining_accounts(self) -> None:
        self._write([self._account("a@example.com"), self._account("b@example.com")])
        sess = mock.Mock()
        sess.ensure_auth.return_value = {
            "organizations": [{"resourceKey": org_migrate.config.ORG_KEY}],
        }
        sess.data = {}
        with mock.patch.object(org_migrate, "session_from_record", return_value=sess), \
             mock.patch.object(org_migrate, "_set_pref_org", side_effect=[OSError("disk error"), None]):
            report = org_migrate.migrate_file(self.json_path, delay_s=0)
        self.assertEqual(report["counts"], {"error": 1, "already": 1})
