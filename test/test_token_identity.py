"""Offline owner/collision regressions; every record is a temporary fake."""
from contextlib import ExitStack
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from moneymin import account_bans, account_transfer, config, minute_api, token_store, upload_storage
from moneymin.web import server

A = "a.b@example.invalid"
B = "a_b@example.invalid"


def record(email=A, uid="uid-a"):
    return {"email": email, "localId": uid, "idToken": "fake-id",
            "refreshToken": "fake-refresh", "expiresIn": "3600", "expires_at": 9999999999}


class TokenIdentityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="token-owner-fixture-")))
        self.secrets = self.root / "secrets"
        self.secrets.mkdir()
        self.data = self.root / "data"
        self.data.mkdir()
        self.stack.enter_context(patch.object(config, "SECRETS_DIR", self.secrets))
        self.stack.enter_context(patch.object(config, "DATA_DIR", self.data))
        self.stack.enter_context(patch.object(minute_api, "_request", side_effect=AssertionError("unexpected provider call")))
        self.stack.enter_context(patch("moneymin.account_bans.require_not_banned"))

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path.read_bytes()

    def login(self, email, *, uid="uid-b", password="  password exact  "):
        with patch.object(minute_api, "_request", return_value=(200, json.dumps(record(email, uid)))) as provider:
            value = minute_api.login(email, password)
        self.assertEqual(provider.call_args.kwargs["body"]["password"], password)
        return value

    def test_hash_separates_collision_and_normalizes(self):
        self.assertEqual(token_store.legacy_path(self.secrets, A), token_store.legacy_path(self.secrets, B))
        self.assertNotEqual(config.token_path(A), config.token_path(B))
        self.assertEqual(config.token_path(" A.B@EXAMPLE.INVALID "), config.token_path(A))
        self.assertEqual(config.token_path(A).name, "token_" + hashlib.sha256(A.encode()).hexdigest() + ".json")

    def test_legacy_owner_migrates_by_exact_copy_and_preserves_original(self):
        legacy = token_store.legacy_path(self.secrets, A)
        before = self.write(legacy, record())
        session = minute_api.Session.from_email(A, live=False)
        self.assertEqual(session.token_file, config.token_path(A))
        self.assertEqual(session.token_file.read_bytes(), before)
        self.assertEqual(legacy.read_bytes(), before)

    def test_foreign_missing_corrupt_legacy_reject_before_refresh(self):
        legacy = token_store.legacy_path(self.secrets, B)
        values = [json.dumps(record(A)).encode(), json.dumps({"idToken": "x"}).encode(), b"{broken"]
        for payload in values:
            with self.subTest(payload=payload):
                legacy.write_bytes(payload)
                with patch.object(minute_api, "_refresh") as refresh, self.assertRaises(minute_api.AuthError):
                    minute_api.Session.from_email(B)
                refresh.assert_not_called()
                self.assertFalse(config.token_path(B).exists())
                self.assertEqual(legacy.read_bytes(), payload)

    def test_fresh_login_b_is_allowed_beside_foreign_legacy_a(self):
        legacy = token_store.legacy_path(self.secrets, A)
        before = self.write(legacy, record(A))
        self.login(B)
        self.assertEqual(legacy.read_bytes(), before)
        self.assertEqual(token_store.read_file(config.token_path(B))["email"], B)
        self.assertEqual(minute_api.Session.from_email(A, live=False).email, A)
        self.assertEqual(minute_api.Session.from_email(B, live=False).email, B)
        self.assertEqual(set(token_store.records(self.secrets)), {A, B})

    def test_fresh_login_preserves_missing_owner_or_corrupt_legacy(self):
        legacy = token_store.legacy_path(self.secrets, B)
        for payload in (b"{broken", b'{"refreshToken":"fake"}'):
            with self.subTest(payload=payload):
                legacy.write_bytes(payload)
                config.token_path(B).unlink(missing_ok=True)
                self.login(B)
                self.assertEqual(legacy.read_bytes(), payload)

    def test_corrupt_or_foreign_primary_blocks_read_login_register_without_fallback(self):
        self.write(token_store.legacy_path(self.secrets, A), record())
        primary = config.token_path(A)
        for payload in (b"{broken", json.dumps(record(B, "uid-b")).encode(), b'{"idToken":"fake"}'):
            with self.subTest(payload=payload):
                primary.write_bytes(payload)
                with patch.object(minute_api, "_request") as provider:
                    for action in (lambda: minute_api.Session.from_email(A),
                                   lambda: minute_api.login(A, "password"),
                                   lambda: minute_api.register(A, "password")):
                        with self.assertRaises(minute_api.AuthError):
                            action()
                    provider.assert_not_called()
                self.assertEqual(primary.read_bytes(), payload)
                self.assertNotIn(A, token_store.records(self.secrets))

    def test_provider_wrong_missing_or_invalid_owner_never_persists(self):
        for value in (record(A), {k: v for k, v in record(B, "uid-b").items() if k != "email"},
                      {**record(B, "uid-b"), "email": [B]}):
            with self.subTest(value=value), patch.object(minute_api, "_request", return_value=(200, json.dumps(value))):
                with self.assertRaises(minute_api.AuthError):
                    minute_api.login(B, "password")
                self.assertFalse(config.token_path(B).exists())

    def test_login_same_owner_changed_uid_preserves_existing_primary(self):
        primary = config.token_path(A)
        before = self.write(primary, record())
        with patch.object(minute_api, "_request", return_value=(200, json.dumps(record(A, "wrong-uid")))):
            with self.assertRaises(minute_api.AuthError):
                minute_api.login(A, "password")
        self.assertEqual(primary.read_bytes(), before)

    def test_exclusive_publication_preserves_concurrent_destination(self):
        primary = config.token_path(A)
        real_link = token_store.os.link
        foreign = json.dumps(record(B, "uid-b")).encode()
        def race(source, destination):
            Path(destination).write_bytes(foreign)
            return real_link(source, destination)
        with patch.object(token_store.os, "link", side_effect=race):
            with self.assertRaises(token_store.TokenStoreError):
                token_store.save(self.secrets, A, record())
        self.assertEqual(primary.read_bytes(), foreign)
        self.assertEqual(list(self.secrets.glob("*.tmp")), [])

    def test_changed_legacy_bytes_are_rejected_before_publication(self):
        primary = config.token_path(A)
        expected = record()
        with self.assertRaises(token_store.TokenStoreError):
            token_store._publish_exclusive(primary, json.dumps(record(B, "uid-b")).encode(), expected)
        self.assertFalse(primary.exists())

    def test_conflicting_owned_legacy_copies_fail_without_migration(self):
        self.write(token_store.legacy_path(self.secrets, A), record())
        self.write(self.secrets / "token_extra.json", {**record(), "idToken": "different"})
        with self.assertRaises(token_store.TokenStoreError):
            token_store.load(self.secrets, A)
        self.assertFalse(config.token_path(A).exists())
        self.assertNotIn(A, token_store.records(self.secrets))

    def test_explicit_file_load_live_false_never_persists_or_rehomes(self):
        path = self.root / "explicit-access.json"
        before = self.write(path, record())
        with patch.object(token_store, "save_file", side_effect=AssertionError("must not save")):
            session = minute_api.Session.from_file(path, live=False)
        self.assertEqual(session.token_file, path)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(config.token_path(A).exists())

    def test_with_email_refuses_rebinding_and_preserves_caches(self):
        session = minute_api.Session(record())
        session._quota_cache["fake-org"] = (1, {"allowed": True})
        state = copy.deepcopy(session.data)
        with self.assertRaises(minute_api.AuthError):
            session.with_email(B)
        self.assertEqual(session.email, A)
        self.assertEqual(session.data, state)
        self.assertEqual(session._quota_cache, {"fake-org": (1, {"allowed": True})})

    def test_memory_owner_uid_or_context_tampering_fails_before_provider(self):
        for mutate in (lambda s: s.data.update(email=B), lambda s: s.data.update(localId="uid-b"),
                       lambda s: setattr(s, "email", B)):
            with self.subTest(mutate=mutate):
                session = minute_api.Session(record())
                mutate(session)
                with patch.object(minute_api, "_refresh") as refresh, self.assertRaises(minute_api.AuthError):
                    session.refresh()
                refresh.assert_not_called()

    def test_disk_owner_or_uid_swap_rejects_reload_refresh_relogin_preserving_memory(self):
        primary = config.token_path(A)
        self.write(primary, record())
        session = minute_api.Session.from_file(primary, live=False)
        memory = copy.deepcopy(session.data)
        for bad in (record(B, "uid-b"), record(A, "uid-b"), {"email": A}, {"localId": "uid-a"}):
            before = self.write(primary, bad)
            for action in (session._reload_from_disk, session.refresh, session._relogin):
                with self.subTest(bad=bad, action=action), self.assertRaises(minute_api.AuthError):
                    action()
                self.assertEqual(session.data, memory)
                self.assertEqual(primary.read_bytes(), before)

    def test_refresh_uid_mismatch_never_mutates_memory_or_disk_and_never_relogs(self):
        primary = config.token_path(A)
        before = self.write(primary, record())
        session = minute_api.Session.from_file(primary, live=False)
        memory = copy.deepcopy(session.data)
        response = {"id_token": "new-fake-id", "refresh_token": "new-fake-refresh", "expires_in": "3600", "user_id": "uid-b"}
        with patch.object(minute_api, "_request", return_value=(200, json.dumps(response))), patch.object(session, "_relogin") as relogin:
            with self.assertRaises(minute_api.AuthError):
                session.refresh()
        relogin.assert_not_called()
        self.assertEqual(session.data, memory)
        self.assertEqual(primary.read_bytes(), before)

    def test_refresh_same_uid_commits_owned_candidate_after_success(self):
        primary = config.token_path(A)
        self.write(primary, record())
        session = minute_api.Session.from_file(primary, live=False)
        response = {"id_token": "new-fake-id", "refresh_token": "new-fake-refresh", "expires_in": "3600", "user_id": "uid-a"}
        with patch.object(minute_api, "_request", return_value=(200, json.dumps(response))):
            session.refresh()
        self.assertEqual(session.data["idToken"], "new-fake-id")
        self.assertEqual(token_store.read_file(primary)["localId"], "uid-a")

    def test_refresh_missing_blank_typed_or_conflicting_uid_preserves_known_identity(self):
        primary = config.token_path(A)
        before = self.write(primary, record())
        session = minute_api.Session.from_file(primary, live=False)
        memory = copy.deepcopy(session.data)
        base = {"id_token": "new-fake", "refresh_token": "new-refresh", "expires_in": "3600"}
        for uid in (None, "", "   ", True, 7, [], "uid-b"):
            response = dict(base)
            if uid is not None:
                response["user_id"] = uid
            with self.subTest(uid=uid), patch.object(minute_api, "_request", return_value=(200, json.dumps(response))), patch.object(session, "_relogin") as relogin:
                with self.assertRaises(minute_api.AuthError):
                    session.refresh()
                relogin.assert_not_called()
                self.assertEqual(session.data, memory)
                self.assertEqual(primary.read_bytes(), before)

    def test_refresh_learns_demonstrated_uid_when_previously_absent(self):
        payload = {k: v for k, v in record().items() if k != "localId"}
        response = {"id_token": "new-fake", "refresh_token": "new-refresh", "expires_in": "3600", "user_id": "uid-a"}
        session = minute_api.Session(payload)
        with patch.object(minute_api, "_request", return_value=(200, json.dumps(response))):
            session.refresh()
        self.assertEqual(token_store.identity_uid(session.data), "uid-a")
        self.assertEqual(session._identity_uid, "uid-a")

    def test_refresh_does_not_fabricate_missing_uid(self):
        payload = {k: v for k, v in record().items() if k != "localId"}
        response = {"id_token": "new-fake-id", "refresh_token": "new-fake-refresh", "expires_in": "3600"}
        with patch.object(minute_api, "_request", return_value=(200, json.dumps(response))):
            result = minute_api._refresh(payload)
        self.assertIsNot(result, payload)
        self.assertIsNone(token_store.identity_uid(result))
        self.assertEqual(payload["idToken"], "fake-id")

    def test_failed_persistence_preserves_disk_and_original_memory(self):
        primary = config.token_path(A)
        before = self.write(primary, record())
        session = minute_api.Session.from_file(primary, live=False)
        memory = copy.deepcopy(session.data)
        candidate = {**record(), "idToken": "new-fake"}
        with patch.object(minute_api, "_refresh", return_value=candidate), patch.object(token_store, "save_json", side_effect=OSError("fixture disk failure")):
            with self.assertRaises(OSError):
                session.refresh()
        self.assertEqual(session.data, memory)
        self.assertEqual(primary.read_bytes(), before)
        self.assertFalse(session._live)

    def test_removal_deletes_only_proven_owner_and_all_owned_copies(self):
        legacy = token_store.legacy_path(self.secrets, A)
        before = self.write(legacy, record(A))
        token_store.save(self.secrets, B, record(B, "uid-b"))
        token_store.delete(self.secrets, B)
        self.assertEqual(legacy.read_bytes(), before)
        self.assertFalse(config.token_path(B).exists())
        token_store.load(self.secrets, A)
        token_store.delete(self.secrets, A)
        self.assertFalse(legacy.exists())
        self.assertFalse(config.token_path(A).exists())

    def test_import_and_export_keep_formerly_colliding_owners_isolated(self):
        raw = json.dumps([{"email": A, "password": "  exact A  ", "token": record(A)},
                          {"email": B, "password": "  exact B  ", "token": record(B, "uid-b")}])
        saved = {}
        with patch.object(account_transfer.credential_store, "save", side_effect=lambda directory, email, password: saved.update({email: password})), \
             patch.object(account_transfer.credential_store, "lookup", side_effect=lambda directory, email, **kwargs: saved.get(email)):
            result = account_transfer.import_accounts(raw, apply=True, passwords_path=self.secrets / "unused-password-map.json", removed_path=self.data / "removed.json")
            exported = account_transfer.export_accounts(None, {}, set())
        self.assertEqual(result["counts"]["imported"], 2)
        self.assertEqual({r["email"] for r in exported["accounts"]}, {A, B})
        self.assertEqual(saved, {A: "  exact A  ", B: "  exact B  "})

    def test_import_blank_tokens_preserve_existing_files_vault_and_tombstone(self):
        primary = config.token_path(A)
        before = self.write(primary, record())
        removed = self.data / "removed.json"
        tombstone = self.write(removed, {"schema": 1, "emails": [A]})
        for field in ("idToken", "refreshToken", "id_token", "refresh_token"):
            with self.subTest(field=field), patch.object(account_transfer.credential_store, "save") as vault:
                value = {**record(), field: "   "}
                with self.assertRaises(token_store.TokenStoreError):
                    account_transfer.clean_record({"email": A, "password": "  exact  ", "token": value})
                vault.assert_not_called()
                self.assertEqual(primary.read_bytes(), before)
                self.assertEqual(removed.read_bytes(), tombstone)

    def test_import_rejects_missing_token_owner_and_preserves_concurrent_destination(self):
        for token in ({k: v for k, v in record().items() if k != "email"}, record(B, "uid-b")):
            with self.subTest(token=token), self.assertRaises(ValueError):
                account_transfer.clean_record({"email": A, "token": token})
        primary = config.token_path(A)
        foreign = json.dumps(record(B, "uid-b")).encode()
        def conflict(*args):
            primary.write_bytes(foreign)
            raise token_store.TokenStoreError("fixture conflict")
        with patch.object(token_store, "save", side_effect=conflict), self.assertRaises(token_store.TokenStoreError):
            account_transfer._save_record({"email": A, "token": record(), "password": None}, primary,
                                          self.secrets / "unused.json", self.data / "removed.json")
        self.assertEqual(primary.read_bytes(), foreign)

    def test_import_rollback_serializes_writer_and_preserves_its_later_publication(self):
        primary = config.token_path(A)
        started = threading.Event()
        finished = threading.Event()
        failures = []
        other = {**record(), "idToken": "other-writer"}
        def writer():
            started.set()
            try:
                token_store.save(self.secrets, A, other)
            except Exception as exc:
                failures.append(exc)
            finally:
                finished.set()
        thread = threading.Thread(target=writer)
        def fail_vault(*args):
            thread.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(finished.is_set())
            raise OSError("fixture vault failure")
        with patch.object(account_transfer.credential_store, "save", side_effect=fail_vault):
            with self.assertRaises(OSError):
                account_transfer._save_record({"email": A, "token": record(), "password": "exact"}, primary,
                                              self.secrets / "unused.json", self.data / "removed.json")
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(token_store.read_file(primary)["idToken"], "other-writer")

    def test_fingerprint_refuses_conflicting_owner_and_remains_stable_across_rotation(self):
        self.stack.enter_context(patch.object(server, "_removed_accounts", return_value=set()))
        primary = config.token_path(A)
        self.write(primary, record())
        first = server._preflight_fingerprint([A])
        self.write(primary, {**record(), "idToken": "rotated", "refreshToken": "rotated", "expires_at": 42})
        self.assertEqual(server._preflight_fingerprint([A]), first)
        before = self.write(primary, record(B, "uid-b"))
        with self.assertRaises(token_store.TokenStoreError):
            server._preflight_fingerprint([A])
        self.assertEqual(primary.read_bytes(), before)

    def test_ban_b_never_deletes_legacy_a(self):
        legacy = token_store.legacy_path(self.secrets, A)
        before = self.write(legacy, record())
        token_store.save(self.secrets, B, record(B, "uid-b"))
        with patch.object(server.banned_store, "load", return_value={"schema": 1, "accounts": []}), \
             patch.object(server.banned_store, "save"), patch.object(server, "_crowtado_creds", return_value={}), \
             patch.object(server, "_set_account_removed"), patch.object(server, "_load_prefs", return_value={}), \
             patch.object(server, "_save_prefs"), patch.object(server, "_remove_account_data"), \
             patch.object(server.account_bans, "purge_local_records"):
            server._ban_accounts([{"email": B, "restriction_confirmed": True}])
        self.assertEqual(legacy.read_bytes(), before)
        self.assertFalse(config.token_path(B).exists())

    def test_purge_preserves_canonical_path_owner_conflict(self):
        primary = config.token_path(B)
        before = self.write(primary, record(A))
        with patch.object(config, "ROOT", self.root), patch.object(config, "LIBRARY_ROOT", self.root), \
             patch.object(account_bans.credential_store, "delete"):
            account_bans.purge_local_records({A})
        self.assertEqual(primary.read_bytes(), before)

    def test_enumeration_and_journal_migration_never_resurrect_hidden_legacy(self):
        legacy_root = self.root / "library" / "data"
        journals = legacy_root / "sidecars"
        journals.mkdir(parents=True)
        self.write(token_store.legacy_path(self.secrets, A), record())
        config.token_path(A).write_bytes(b"{broken")
        journal = journals / "fake-session.chunk0.json"
        before = self.write(journal, {"account_email": A, "session_id": "fake-session", "chunk_index": 0})
        with patch.object(config, "MEDIA_DATA_DIR", legacy_root):
            destination = upload_storage.journal_directory()
        self.assertFalse((destination / journal.name).exists())
        self.assertEqual(journal.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
