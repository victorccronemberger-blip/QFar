"""Semantic regression fixtures with artificial credentials and no remote effects."""
from contextlib import ExitStack
import copy
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from moneymin.web import server


EMAIL = "local-fixture@example.invalid"
PASSWORD = " fixture-only password áß "
PRIVATE = "FIXTURE_PRIVATE_CANARY https://fixture.invalid/?sig=FIXTURE_PRIVATE_CANARY"


class LocalCredentialInputTests(unittest.TestCase):
    def setUp(self):
        self.client = server.create_app(for_testing=True).test_client()

    def test_registration_and_balance_credentials_reject_nontext_before_any_dependency(self):
        for route, method in (("/api/accounts/register", "POST"),
                              ("/api/balances/credentials", "PUT")):
            for field in ("email", "password"):
                for value in (None, False, True, 7, 1.5, [], {}, [EMAIL]):
                    body = {"email": EMAIL, "password": PASSWORD}
                    body[field] = value
                    with self.subTest(route=route, field=field, value=value), ExitStack() as stack:
                        dependencies = [stack.enter_context(patch.object(owner, name))
                            for owner, name in ((server, "_hostinger_is_configured"),
                                (server, "_full_register_account"), (server, "_list_accounts"),
                                (server.account_bans, "require_not_banned"),
                                (server.crowtado, "login"), (server, "_save_crowtado_cred"))]
                        response = self.client.open(route, method=method, json=body)
                        self.assertEqual(response.status_code, 400)
                        self.assertEqual(response.get_json()["code"], "invalid_credentials")
                        for dependency in dependencies:
                            dependency.assert_not_called()

    def test_registration_passes_exact_password_to_fake_provider(self):
        with patch.object(server, "_hostinger_is_configured", return_value=True), \
             patch.object(server, "_full_register_account",
                          return_value={"steps": {}, "error": None}) as provider:
            response = self.client.post("/api/accounts/register",
                json={"email": " " + EMAIL + " ", "password": PASSWORD, "gender": "female"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(provider.call_args.args[:2], (EMAIL, PASSWORD))
        self.assertEqual(provider.call_args.args[2]["senha"], PASSWORD)

    def test_balance_credentials_pass_exact_password_and_preserve_partial_save_state(self):
        with patch.object(server.account_bans, "require_not_banned"), \
             patch.object(server, "_list_accounts", return_value=[{"email": EMAIL}]), \
             patch.object(server.crowtado, "login") as provider, \
             patch.object(server, "_save_crowtado_cred", side_effect=OSError(PRIVATE)):
            response = self.client.put("/api/balances/credentials",
                json={"email": " " + EMAIL + " ", "password": PASSWORD})
        provider.assert_called_once_with(EMAIL, PASSWORD)
        self.assertEqual(response.status_code, 500)
        self.assertTrue(response.get_json()["partial"])
        self.assertFalse(response.get_json()["ok"])
        self.assertNotIn("FIXTURE_PRIVATE_CANARY", response.get_data(as_text=True))


class LocalDiagnosticPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.client = server.create_app(for_testing=True).test_client()

    def test_account_login_and_org_errors_use_classified_diagnostics(self):
        for stage, code in (("login", "account_login_failed"),
                            ("_resolve_org", "account_organization_unconfirmed")):
            for exception in (RuntimeError(PRIVATE), OSError(PRIVATE), server.AuthError(PRIVATE)):
                with self.subTest(stage=stage, exception=type(exception).__name__), \
                     patch.object(server.account_bans, "require_not_banned"), \
                     patch.object(server, "login", side_effect=exception if stage == "login" else None), \
                     patch.object(server, "_resolve_org",
                                  side_effect=exception if stage == "_resolve_org" else None), \
                     patch.object(server, "_save_crowtado_cred") as save, \
                     patch.object(server, "_set_account_removed") as activate:
                    response = self.client.post("/api/accounts",
                        json={"email": EMAIL, "password": PASSWORD})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["code"], code)
                self.assertNotIn("FIXTURE_PRIVATE_CANARY", response.get_data(as_text=True))
                self.assertIn("reason", response.get_json()["issue"])
                save.assert_not_called()
                activate.assert_not_called()

    def test_balance_login_error_never_echoes_provider_exception(self):
        for exception in (server.crowtado.CrowtadoError(PRIVATE), RuntimeError(PRIVATE), OSError(PRIVATE)):
            with self.subTest(exception=type(exception).__name__), \
                 patch.object(server.account_bans, "require_not_banned"), \
                 patch.object(server, "_list_accounts", return_value=[{"email": EMAIL}]), \
                 patch.object(server.crowtado, "login", side_effect=exception), \
                 patch.object(server, "_save_crowtado_cred") as save:
                response = self.client.put("/api/balances/credentials",
                    json={"email": EMAIL, "password": PASSWORD})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.get_json()["code"], "crowtado_login_failed")
            self.assertNotIn("FIXTURE_PRIVATE_CANARY", response.get_data(as_text=True))
            save.assert_not_called()

    def test_payout_method_background_failure_omits_all_exception_content(self):
        original = copy.deepcopy(server._PAYOUT_METHOD_STATE)
        try:
            with server._PAYOUT_METHOD_LOCK:
                server._PAYOUT_METHOD_STATE.update(state="running", total=1, done=0, results=[])
            with patch.object(server, "_wise_cleanup_snapshot", return_value={"pending": False}), \
                 patch.object(server.crowtado, "configurar_metodo_saque", side_effect=OSError(PRIVATE)):
                server._payout_method_run({EMAIL: PASSWORD}, "dots", "Fixture Name", "destination@example.invalid")
            snapshot = server._payout_method_snapshot()
            self.assertNotIn("FIXTURE_PRIVATE_CANARY", json.dumps(snapshot))
            self.assertFalse(snapshot["results"][0]["ok"])
            self.assertEqual(snapshot["done"], 1)
            self.assertIn("code", snapshot["results"][0])
        finally:
            with server._PAYOUT_METHOD_LOCK:
                server._PAYOUT_METHOD_STATE.clear()
                server._PAYOUT_METHOD_STATE.update(original)

    def test_preflight_dependency_error_does_not_echo_private_exception(self):
        with patch.object(server, "_list_accounts", return_value=[]), \
             patch.object(server, "_local_dataset_provider", return_value="ego4d"), \
             patch.object(server.readiness, "campaign_readiness", side_effect=RuntimeError(PRIVATE)), \
             patch.object(server.recovery, "snapshot", return_value={"items": []}), \
             patch.object(server, "_storage_snapshot", return_value={"free_bytes": 20 * 1024 ** 3}):
            response = self.client.post("/api/campaigns/preflight", json={})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["ok"])
        self.assertEqual(response.get_json()["readiness"]["code"], "readiness_unavailable")
        self.assertNotIn("FIXTURE_PRIVATE_CANARY", response.get_data(as_text=True))

    def test_log_session_query_errors_keep_transient_counts_and_private_diagnostics(self):
        log = {"items": [{"accounts": [{"email": EMAIL, "session_id": "fixture-session",
                                       "org_key": "fixture-org", "uploads": [{}, {}]}]}]}
        for stage, code in (("access", "session_access_unavailable"),
                            ("status", "session_status_unavailable")):
            with self.subTest(stage=stage), \
                 patch.object(server, "_log_path", return_value=Path("fixture-log.json")), \
                 patch.object(server, "_read_campaign_log", return_value=log), \
                 patch.object(server.Session, "from_email",
                              side_effect=server.AuthError(PRIVATE) if stage == "access" else None), \
                 patch.object(server.campaign, "session_result", side_effect=RuntimeError(PRIVATE)):
                response = self.client.post("/api/logs/campaign_fixture.json/status", json={})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["results"][0]["code"], code)
            self.assertEqual(response.get_json()["summary"]["transient_errors"], 2)
            self.assertEqual(response.get_json()["sessions"]["errors"], 1)
            self.assertNotIn("FIXTURE_PRIVATE_CANARY", response.get_data(as_text=True))

    def test_unexpected_bulk_exception_preserves_uncertainty_stops_and_keeps_private_receipt(self):
        original = copy.deepcopy(server._WITHDRAW_BULK_STATE)
        try:
            with tempfile.TemporaryDirectory() as folder, \
                 patch.object(server.config, "DATA_DIR", Path(folder)), \
                 patch.object(server, "_withdraw_once", side_effect=RuntimeError(PRIVATE)) as provider:
                with server._WITHDRAW_BULK_LOCK:
                    server._WITHDRAW_BULK_STATE.update(state="running", total=2, done=0, results=[])
                server._withdraw_bulk_run({EMAIL: PASSWORD, "second@example.invalid": "fixture-only"})
                snapshot = copy.deepcopy(server._WITHDRAW_BULK_STATE)
                saved = json.loads((Path(folder) / "withdraw_bulk_state.json").read_text(encoding="utf-8"))
            self.assertNotIn("FIXTURE_PRIVATE_CANARY", json.dumps(snapshot))
            self.assertNotIn("FIXTURE_PRIVATE_CANARY", json.dumps(saved))
            self.assertEqual(snapshot["state"], "error")
            self.assertEqual(snapshot["done"], 1)
            provider.assert_called_once_with(EMAIL, PASSWORD)
            self.assertIn("inconclusiva", snapshot["message"])
        finally:
            with server._WITHDRAW_BULK_LOCK:
                server._WITHDRAW_BULK_STATE.clear()
                server._WITHDRAW_BULK_STATE.update(original)

    def test_known_preattempt_withdrawal_error_is_classified_without_private_text(self):
        original_times = dict(server._WITHDRAW_LAST_REQUEST)
        original_in_flight = set(server._WITHDRAW_IN_FLIGHT)
        error = server.crowtado.CrowtadoError(PRIVATE)
        error.withdrawal_attempted = False
        try:
            with server._WITHDRAW_LOCK:
                server._WITHDRAW_LAST_REQUEST.clear()
                server._WITHDRAW_IN_FLIGHT.clear()
            with patch.object(server, "_load_withdraw_cooldowns_locked"), \
                 patch.object(server, "save_json"), \
                 patch.object(server.crowtado, "solicitar_link_saque",
                              side_effect=error):
                result, status = server._withdraw_once_locked(EMAIL, PASSWORD)
            self.assertEqual(status, 400)
            self.assertFalse(result["ok"])
            self.assertNotIn("FIXTURE_PRIVATE_CANARY", json.dumps(result))
            self.assertEqual(result["code"], "withdrawal_not_requested")
        finally:
            with server._WITHDRAW_LOCK:
                server._WITHDRAW_LAST_REQUEST.clear()
                server._WITHDRAW_LAST_REQUEST.update(original_times)
                server._WITHDRAW_IN_FLIGHT.clear()
                server._WITHDRAW_IN_FLIGHT.update(original_in_flight)


class LocalAuthoritativeStateTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(server.config, "DATA_DIR", self.root))
        self.patches.enter_context(patch.object(server, "PREFS_PATH", self.root / "webui_prefs.json"))
        self.patches.enter_context(patch.object(server, "BALANCES_PATH", self.root / "balances.json"))
        self.patches.enter_context(patch.object(server.account_bans, "banned_emails", return_value=set()))
        self.client = server.create_app(for_testing=True).test_client()

    def test_all_six_authoritative_documents_reject_bad_bytes_without_replacement(self):
        cases = (("webui_prefs.json", server._load_prefs),
                 ("removed_accounts.json", server._removed_accounts),
                 ("balances.json", server._load_balances),
                 ("withdraw_last_result.json", lambda: server._last_withdrawal_receipt({EMAIL})),
                 ("withdraw_request_times.json", server._load_withdraw_cooldowns_locked),
                 ("withdraw_bulk_state.json", server._load_withdraw_bulk_locked))
        for filename, reader in cases:
            for raw in (b'{"FIXTURE_PRIVATE_CANARY":', b'[]', b'null',
                        b'{"a":NaN}', b'{"a":1,"a":2}', b'\xff'):
                with self.subTest(filename=filename, raw=raw):
                    path = self.root / filename
                    path.write_bytes(raw)
                    with self.assertRaises(server.JsonStateError) as caught:
                        reader()
                    self.assertNotIn("FIXTURE_PRIVATE_CANARY", str(caught.exception))
                    self.assertNotIn(str(path), str(caught.exception))
                    self.assertEqual(path.read_bytes(), raw)
                    path.unlink()

    def test_critical_nested_shapes_are_not_coerced_to_empty_state(self):
        cases = (("webui_prefs.json", server._load_prefs, {"selected_accounts": {}}),
                 ("webui_prefs.json", server._load_prefs, {"org_keys": []}),
                 ("webui_prefs.json", server._load_prefs, {"holoassist_enabled": "false"}),
                 ("removed_accounts.json", server._removed_accounts, {"emails": EMAIL}),
                 ("removed_accounts.json", server._removed_accounts, {"emails": [None]}),
                 ("balances.json", server._load_balances, {EMAIL: 1}),
                 ("withdraw_request_times.json", server._load_withdraw_cooldowns_locked, {EMAIL: True}),
                 ("withdraw_request_times.json", server._load_withdraw_cooldowns_locked, {EMAIL: "123"}),
                 ("withdraw_request_times.json", server._load_withdraw_cooldowns_locked, {EMAIL: 10 ** 1000}),
                 ("withdraw_bulk_state.json", server._load_withdraw_bulk_locked, {"results": [None]}),
                 ("withdraw_bulk_state.json", server._load_withdraw_bulk_locked, {"results": [], "state": []}),
                 ("withdraw_bulk_state.json", server._load_withdraw_bulk_locked,
                  {"results": [], "total": True}))
        for filename, reader, value in cases:
            with self.subTest(filename=filename, value=value):
                path = self.root / filename
                raw = json.dumps(value).encode("utf-8")
                path.write_bytes(raw)
                with self.assertRaises(server.JsonStateError):
                    reader()
                self.assertEqual(path.read_bytes(), raw)
                path.unlink()

    def test_corrupt_preferences_reads_and_updates_return_private_conflict(self):
        path = self.root / "webui_prefs.json"
        raw = b'{"FIXTURE_PRIVATE_CANARY":'
        path.write_bytes(raw)
        for method in ("GET", "PUT"):
            with self.subTest(method=method):
                response = self.client.open("/api/preferences", method=method,
                    json={"future_setting": 1} if method == "PUT" else None)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.get_json()["code"], "local_state_unreadable")
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertNotIn("FIXTURE_PRIVATE_CANARY", response.get_data(as_text=True))
                self.assertEqual(path.read_bytes(), raw)

    def test_invalid_preference_candidate_cannot_replace_valid_state(self):
        path = self.root / "webui_prefs.json"
        raw = b'{"selected_accounts":[],"future_setting":1}'
        path.write_bytes(raw)
        response = self.client.put("/api/preferences", json={"selected_accounts": {}})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(path.read_bytes(), raw)

    def test_nonfinite_candidate_cannot_poison_unknown_preferences_or_balance_fields(self):
        for filename, writer, value in (("webui_prefs.json", server._save_prefs, {"future": float("nan")}),
                                       ("balances.json", server._save_balances, {EMAIL: {"future": float("inf")}})):
            with self.subTest(filename=filename):
                path = self.root / filename
                path.write_bytes(b'{}')
                with self.assertRaises(server.JsonStateError):
                    writer(value)
                self.assertEqual(path.read_bytes(), b'{}')

    def test_corrupt_balances_prevent_refresh_before_account_migration_or_worker(self):
        path = self.root / "balances.json"
        raw = b'{"FIXTURE_PRIVATE_CANARY":'
        path.write_bytes(raw)
        with patch.object(server, "_withdraw_bulk_snapshot", return_value={"state": "idle"}), \
             patch.object(server, "_list_accounts") as accounts, \
             patch.object(server, "_configured_crowtado_creds") as credentials, \
             patch.object(server.BALANCES_RUNNER, "start") as worker:
            response = self.client.post("/api/balances/refresh", json={})
        self.assertEqual(response.status_code, 409)
        accounts.assert_not_called()
        credentials.assert_not_called()
        worker.assert_not_called()
        self.assertEqual(path.read_bytes(), raw)

    def test_direct_preference_and_balance_saves_preserve_unreadable_previous_state(self):
        for filename, writer in (("webui_prefs.json", server._save_prefs),
                                 ("balances.json", server._save_balances)):
            with self.subTest(filename=filename):
                path = self.root / filename
                raw = b'{"FIXTURE_PRIVATE_CANARY":'
                path.write_bytes(raw)
                with self.assertRaises(server.JsonStateError):
                    writer({})
                self.assertEqual(path.read_bytes(), raw)

    def test_unreadable_receipt_or_cooldown_stops_before_new_journal_and_provider(self):
        for filename in ("withdraw_last_result.json", "withdraw_request_times.json"):
            with self.subTest(filename=filename):
                path = self.root / filename
                raw = b'{"FIXTURE_PRIVATE_CANARY":'
                path.write_bytes(raw)
                with patch.object(server, "save_json") as save, \
                     patch.object(server.crowtado, "solicitar_link_saque") as provider:
                    with self.assertRaises(server.JsonStateError):
                        server._withdraw_once_locked(EMAIL, PASSWORD)
                save.assert_not_called()
                provider.assert_not_called()
                self.assertEqual(path.read_bytes(), raw)
                path.unlink()

    def test_corrupt_bulk_state_blocks_worker_even_if_it_was_loaded_before(self):
        path = self.root / "withdraw_bulk_state.json"
        raw = b'{"FIXTURE_PRIVATE_CANARY":'
        path.write_bytes(raw)
        with patch.object(server, "_WITHDRAW_BULK_LOADED", True), \
             patch.object(server.threading, "Thread") as worker, \
             patch.object(server, "save_json") as save:
            with self.assertRaises(server.JsonStateError):
                server._start_withdraw_worker({EMAIL: PASSWORD}, None)
        worker.assert_not_called()
        save.assert_not_called()
        self.assertEqual(path.read_bytes(), raw)

    def test_unreadable_receipt_or_cooldown_prevents_bulk_state_replacement(self):
        bulk = self.root / "withdraw_bulk_state.json"
        before = b'{"state":"done","total":1,"done":1,"results":[]}'
        for filename in ("withdraw_last_result.json", "withdraw_request_times.json"):
            with self.subTest(filename=filename):
                bulk.write_bytes(before)
                path = self.root / filename
                raw = b'{"FIXTURE_PRIVATE_CANARY":'
                path.write_bytes(raw)
                with patch.object(server.threading, "Thread") as worker:
                    with self.assertRaises(server.JsonStateError):
                        server._start_withdraw_worker({EMAIL: PASSWORD}, None)
                worker.assert_not_called()
                self.assertEqual(path.read_bytes(), raw)
                self.assertEqual(bulk.read_bytes(), before)
                path.unlink()

    def test_withdrawal_exception_without_attempt_evidence_remains_unknown(self):
        original_times = dict(server._WITHDRAW_LAST_REQUEST)
        original_in_flight = set(server._WITHDRAW_IN_FLIGHT)
        try:
            with server._WITHDRAW_LOCK:
                server._WITHDRAW_LAST_REQUEST.clear()
                server._WITHDRAW_IN_FLIGHT.clear()
            with patch.object(server, "_load_withdraw_cooldowns_locked"), \
                 patch.object(server, "save_json"), \
                 patch.object(server, "_invalidate_balance_after_withdrawal") as invalidate, \
                 patch.object(server.crowtado, "solicitar_link_saque",
                              side_effect=server.crowtado.CrowtadoError(PRIVATE)):
                result, status = server._withdraw_once_locked(EMAIL, PASSWORD)
            self.assertEqual(status, 409)
            self.assertFalse(result["ok"])
            self.assertEqual(result["result"]["status"], "unknown")
            self.assertNotIn("FIXTURE_PRIVATE_CANARY", json.dumps(result))
            invalidate.assert_called_once()
        finally:
            with server._WITHDRAW_LOCK:
                server._WITHDRAW_LAST_REQUEST.clear()
                server._WITHDRAW_LAST_REQUEST.update(original_times)
                server._WITHDRAW_IN_FLIGHT.clear()
                server._WITHDRAW_IN_FLIGHT.update(original_in_flight)
