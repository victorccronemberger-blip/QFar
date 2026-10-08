"""Local-session boundary tests; positive operations use fake providers and storage."""
import ast
from contextlib import ExitStack
import os
from pathlib import Path
import re
import unittest
from unittest.mock import Mock, patch

from moneymin import web
from moneymin.web import server


TOKEN = "fixture-only-local-api-session"


def _product_operations():
    source = Path(server.__file__).read_text(encoding="utf-8-sig")
    factory = next(node for node in ast.parse(source).body
                   if isinstance(node, ast.FunctionDef) and node.name == "create_app")
    result = set()
    for function in factory.body:
        if not isinstance(function, ast.FunctionDef):
            continue
        for decorator in function.decorator_list:
            if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                    and isinstance(decorator.func.value, ast.Name)
                    and decorator.func.value.id == "app"
                    and decorator.func.attr in {"get", "post", "put", "delete", "patch"}):
                result.add((decorator.args[0].value, decorator.func.attr.upper()))
    return result


def _fixture_path(path):
    replacements = {"email": "fixture@example.invalid", "name": "fixture-log.json",
                    "profile_id": "fixture-profile"}
    return re.sub(r"<([^>]+)>",
                  lambda match: replacements.get(match.group(1).split(":")[-1], "fixture"), path)


class LocalApiSessionBoundaryTests(unittest.TestCase):
    def test_all_product_operations_reject_bad_sessions_before_later_hooks_and_handlers(self):
        with patch.dict(os.environ, {"QMONEY_LOCAL_API_TOKEN": TOKEN}):
            application = server.create_app()
        calls = []

        def forbidden(*args, **kwargs):
            calls.append("effect")
            raise AssertionError("Authentication must stop before request validation or effects.")

        for endpoint in application.view_functions:
            application.view_functions[endpoint] = forbidden
        hooks = application.before_request_funcs[None]
        application.before_request_funcs[None] = hooks[:1] + [forbidden for _ in hooks[1:]]
        operations = _product_operations()
        registered = {(rule.rule, method) for rule in application.url_map.iter_rules()
                      for method in rule.methods - {"HEAD", "OPTIONS"}}
        self.assertEqual(len(operations), 89)
        nymeria_operations = {
            ("/api/library/nymeria", "GET"), ("/api/library/nymeria/sequences", "GET"),
            ("/api/library/nymeria/operation", "GET"), ("/api/library/nymeria/stop", "POST"),
            ("/api/library/nymeria/import", "POST"), ("/api/library/nymeria/sync", "POST"),
            ("/api/library/nymeria/plan", "POST"), ("/api/library/nymeria/download", "POST"),
        }
        self.assertTrue(nymeria_operations <= operations)
        self.assertIn(("/api/library/ego4d/prepared", "GET"), operations)
        self.assertIn(("/api/storage/library/items", "GET"), operations)
        self.assertIn(("/api/accounts/proxies", "GET"), operations)
        self.assertIn(("/api/accounts/proxies/import", "POST"), operations)
        self.assertIn(("/api/accounts/bulk-register/stop", "POST"), operations)
        self.assertIn(("/api/accounts/<email>/resume", "POST"), operations)
        self.assertIn(("/api/campaigns/starts/<start_id>", "GET"), operations)
        self.assertEqual(registered, operations)
        client = application.test_client()
        for path, method in sorted(operations):
            for token in (None, "fixture-other-session"):
                with self.subTest(path=path, method=method, token_present=token is not None):
                    response = client.open(_fixture_path(path), method=method,
                        headers={"X-QMoney-Session": token} if token else {},
                        data="{malformed-fixture-json", content_type="application/json")
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.get_json(), {"error": "Cliente local não autorizado."})
                    self.assertEqual(calls, [])

    def test_correct_session_reaches_only_replaced_handlers_on_all_product_operations(self):
        with patch.dict(os.environ, {"QMONEY_LOCAL_API_TOKEN": TOKEN}):
            # A configured token must still be enforced when this explicit fixture flag is set.
            application = server.create_app(for_testing=True)
        calls = []

        def canary(**kwargs):
            calls.append(server.request.endpoint)
            return {"fixture_only": True}

        for endpoint in application.view_functions:
            application.view_functions[endpoint] = canary
        application.before_request_funcs[None] = application.before_request_funcs[None][:1]
        client = application.test_client()
        for path, method in sorted(_product_operations()):
            with self.subTest(path=path, method=method):
                previous = len(calls)
                denied = client.open(_fixture_path(path), method=method, json={})
                self.assertEqual(denied.status_code, 401)
                response = client.open(_fixture_path(path), method=method, json={},
                                       headers={"X-QMoney-Session": TOKEN})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json(), {"fixture_only": True})
                self.assertEqual(len(calls), previous + 1)

    def test_normal_factory_requires_token_in_source_and_frozen_before_initialization(self):
        for frozen in (False, True):
            with self.subTest(frozen=frozen), \
                 patch.dict(os.environ, {"QMONEY_LOCAL_API_TOKEN": ""}), \
                 patch.object(server.sys, "frozen", frozen, create=True), \
                 patch.object(server, "CatalogLoader") as loader:
                with self.assertRaises(RuntimeError):
                    server.create_app()
                loader.assert_not_called()

    def test_unauthenticated_factory_fixture_is_explicit_source_only(self):
        with patch.dict(os.environ, {"QMONEY_LOCAL_API_TOKEN": ""}), \
             patch.object(server.sys, "frozen", False, create=True):
            application = server.create_app(for_testing=True)
        response = application.test_client().get("/api/health")
        self.assertEqual(response.status_code, 200)
        with patch.dict(os.environ, {"QMONEY_LOCAL_API_TOKEN": ""}), \
             patch.object(server.sys, "frozen", True, create=True):
            with self.assertRaises(RuntimeError):
                server.create_app(for_testing=True)

    def test_factory_fixture_flag_requires_a_boolean(self):
        for value in (None, 0, 1, "false", "true", [], {}):
            with self.subTest(value=value), patch.dict(os.environ, {"QMONEY_LOCAL_API_TOKEN": TOKEN}):
                with self.assertRaises(TypeError):
                    server.create_app(for_testing=value)


class LocalApiBootstrapTests(unittest.TestCase):
    def test_invalid_bootstrap_stops_before_tls_parent_workers_and_listener(self):
        cases = [("0.0.0.0", 8876, TOKEN), ("::", 8876, TOKEN),
                 ("192.0.2.1", 8876, TOKEN), ("remote.invalid", 8876, TOKEN),
                 (None, 8876, TOKEN), ("127.0.0.1", True, TOKEN),
                 ("127.0.0.1", 0, TOKEN), ("127.0.0.1", 65536, TOKEN),
                 ("127.0.0.1", "8876", TOKEN), ("127.0.0.1", 8876.0, TOKEN),
                 ("127.0.0.1", 8876, "")]
        for host, port, token in cases:
            with self.subTest(host=host, port=port, token_present=bool(token)), ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, {"QMONEY_LOCAL_API_TOKEN": token}))
                effects = [stack.enter_context(patch.object(owner, name)) for owner, name in
                           ((web, "_harden_stdio"), (web.tls, "configure_environment"),
                            (web, "_watch_parent"), (web, "create_app"),
                            (web.threading, "Thread"), (web, "_serve"))]
                with self.assertRaises((ValueError, RuntimeError)):
                    web.run_webui(host=host, port=port, parent_pid=123)
                for effect in effects:
                    effect.assert_not_called()

    def test_direct_serve_requires_loopback_port_and_session_before_listener(self):
        for host, port, token in (("0.0.0.0", 8876, TOKEN), ("127.0.0.1", 0, TOKEN),
                                  ("127.0.0.1", 8876, "")):
            with self.subTest(host=host, port=port, token_present=bool(token)), \
                 patch.dict(os.environ, {"QMONEY_LOCAL_API_TOKEN": token}):
                application = Mock()
                with self.assertRaises((ValueError, RuntimeError)):
                    web._serve(application, host, port, False)
                application.run.assert_not_called()

    def test_valid_loopback_hosts_keep_explicit_port_and_no_reloader(self):
        for host, expected in (("localhost", "127.0.0.1"), ("127.0.0.1", "127.0.0.1"),
                               ("127.0.0.2", "127.0.0.2"), ("::1", "::1")):
            with self.subTest(host=host), patch.dict(os.environ, {"QMONEY_LOCAL_API_TOKEN": TOKEN}):
                application = Mock()
                application.config = {"QMONEY_LOCAL_API_AUTHENTICATED": True}
                web._serve(application, host, 8876, False)
                application.run.assert_called_once_with(host=expected, port=8876, threaded=True,
                                                       use_reloader=False)

    def test_unauthenticated_in_memory_fixture_cannot_be_exposed_after_environment_changes(self):
        with patch.dict(os.environ, {"QMONEY_LOCAL_API_TOKEN": ""}), \
             patch.object(server.sys, "frozen", False, create=True):
            application = server.create_app(for_testing=True)
        with patch.dict(os.environ, {"QMONEY_LOCAL_API_TOKEN": TOKEN}), \
             patch.object(application, "run") as listener:
            with self.assertRaises(RuntimeError):
                web._serve(application, "127.0.0.1", 8876, False)
            listener.assert_not_called()


class LocalApiSelectedContractTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {"QMONEY_LOCAL_API_TOKEN": TOKEN}):
            self.client = server.create_app().test_client()
        self.headers = {"X-QMoney-Session": TOKEN}

    def test_account_credentials_reject_other_types_before_any_dependency(self):
        for field in ("email", "password"):
            for value in (None, False, True, 7, 1.5, [], {}, ["fixture@example.invalid"]):
                body = {"email": "fixture@example.invalid", "password": "fixture-only"}
                body[field] = value
                with self.subTest(field=field, value=value), ExitStack() as stack:
                    dependencies = [stack.enter_context(patch.object(owner, name)) for owner, name in
                                    ((server.account_bans, "require_not_banned"), (server, "login"),
                                     (server, "_resolve_org"), (server, "_save_crowtado_cred"),
                                     (server, "_set_account_removed"))]
                    response = self.client.post("/api/accounts", json=body, headers=self.headers)
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.get_json()["code"], "invalid_credentials")
                    for dependency in dependencies:
                        dependency.assert_not_called()

    def test_account_credentials_preserve_password_exactly(self):
        password = " fixture-only-Password:áß<>! "
        with patch.object(server.account_bans, "require_not_banned"), \
             patch.object(server, "login") as login, \
             patch.object(server, "_resolve_org", return_value="FIXTURE-ORG"), \
             patch.object(server, "_save_crowtado_cred") as save, \
             patch.object(server, "_set_account_removed"):
            response = self.client.post("/api/accounts", headers=self.headers,
                json={"email": " fixture@example.invalid ", "password": password})
        self.assertEqual(response.status_code, 200)
        login.assert_called_once_with("fixture@example.invalid", password)
        save.assert_called_once_with("fixture@example.invalid", password)

    def test_task_dependency_errors_do_not_export_exception_or_email_in_response_or_log(self):
        marker = "FIXTURE_PRIVATE_CANARY https://fixture.invalid/?sig=FIXTURE_PRIVATE_CANARY"
        email = "fixture-private-email@example.invalid"
        with patch.object(server.Session, "from_email", side_effect=server.AuthError(marker)), \
             self.assertLogs("moneymin.web.server", level="WARNING") as logs:
            response = self.client.get("/api/tasks", query_string={"email": email}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "catalog_account_unavailable")
        self.assertEqual(response.get_json()["issue"]["email"], email)
        self.assertEqual(response.get_json()["issue"]["code"], "unknown")
        for private in (marker, "FIXTURE_PRIVATE_CANARY"):
            self.assertNotIn(private, response.get_data(as_text=True))
            self.assertNotIn(private, str(logs.output))
        self.assertNotIn(email, str(logs.output))

        for error in (RuntimeError(marker), OSError(marker)):
            with self.subTest(error=type(error).__name__), \
                 patch.object(server.Session, "from_email", side_effect=error), \
                 self.assertLogs("moneymin.web.server", level="WARNING") as logs:
                response = self.client.get("/api/tasks", query_string={"email": email}, headers=self.headers)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.get_json()["code"], "task_catalog_unavailable")
            for private in (marker, email, "FIXTURE_PRIVATE_CANARY"):
                self.assertNotIn(private, response.get_data(as_text=True))
                self.assertNotIn(private, str(logs.output))

    def test_readiness_dependency_errors_do_not_export_exception_payload(self):
        marker = "FIXTURE_PRIVATE_CANARY https://fixture.invalid/?sig=FIXTURE_PRIVATE_CANARY"
        for error in (ValueError(marker), RuntimeError(marker), OSError(marker)):
            with self.subTest(error=type(error).__name__), \
                 patch.object(server.readiness, "campaign_readiness", side_effect=error):
                response = self.client.get("/api/readiness", headers=self.headers)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.get_json()["code"], "readiness_unavailable")
            self.assertNotIn("FIXTURE_PRIVATE_CANARY", response.get_data(as_text=True))

    def test_task_validation_errors_are_fixed_without_copying_exception_text(self):
        marker = "FIXTURE_PRIVATE_CANARY"
        for dependency, code in (("_local_dataset_provider", "invalid_task_selection"),
                                 ("_parse_duration_range", "invalid_task_duration")):
            with self.subTest(dependency=dependency), \
                 patch.object(server, dependency, side_effect=ValueError(marker)), \
                 patch.object(server.Session, "from_email") as session:
                response = self.client.get("/api/tasks?email=fixture@example.invalid", headers=self.headers)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.get_json()["code"], code)
            self.assertNotIn(marker, response.get_data(as_text=True))
            session.assert_not_called()
