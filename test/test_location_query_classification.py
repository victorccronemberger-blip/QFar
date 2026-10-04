"""Offline U05a regression at the exact location-header expression boundary.

Only a boolean canary and an inert transport stub are used. No location sample,
product import, owner lookup, configuration, auth or policy preparation runs.
"""
from pathlib import Path
import ast
import copy
from types import SimpleNamespace
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "moneymin/minute_api.py"


def location_boolean(transport_method: str, path: str) -> bool:
    text = SOURCE.read_text(encoding="utf-8-sig")
    tree = ast.parse(text, filename=str(SOURCE))
    helper = next(node for node in tree.body
                  if isinstance(node, ast.FunctionDef) and node.name == "_is_geo_route")
    session = next(node for node in tree.body
                   if isinstance(node, ast.ClassDef) and node.name == "Session")
    method = next(node for node in session.body
                  if isinstance(node, ast.FunctionDef) and node.name == transport_method)
    updates = [node for node in ast.walk(method)
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr == "update"
               and any(isinstance(child, ast.keyword) and child.arg == "include_location"
                       for child in ast.walk(node))]
    if len(updates) != 1:
        raise AssertionError("Expected exactly one current profile header update")
    transport_name = "_request" if transport_method == "request" else "_request_detailed"
    transports = [node for node in ast.walk(method)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                  and node.func.id == transport_name]
    if not transports:
        raise AssertionError("Expected current low-level transport calls")
    first_transport = min(transports, key=lambda node: node.lineno)
    flags = []
    forwarded = []

    class BooleanCanary:
        def headers(self, *, include_location):
            if type(include_location) is not bool:
                raise AssertionError("Only a boolean may reach this canary")
            flags.append(include_location)
            return {}

    def transport_stub(url, method, *, headers, body):
        forwarded.append((url, method, dict(headers), body))
        return None

    namespace = {
        "path": path, "headers": {}, "self": SimpleNamespace(email=object()),
        "device_profile": SimpleNamespace(get_profile=lambda _: BooleanCanary()),
        "config": SimpleNamespace(BASE_URL=""), "method": "GET", "body": None,
        transport_name: transport_stub,
    }
    selected = ast.Module(
        body=[copy.deepcopy(helper), ast.Expr(value=copy.deepcopy(updates[0])),
              ast.Expr(value=copy.deepcopy(first_transport))],
        type_ignores=[],
    )
    ast.fix_missing_locations(selected)
    exec(compile(selected, str(SOURCE) + ":isolated_location_boolean", "exec"), namespace)
    if len(flags) != 1 or namespace["headers"] != {}:
        raise AssertionError("Unexpected boolean/header canary activity")
    if forwarded != [(path, "GET", {}, None)]:
        raise AssertionError("Query or fragment was changed before the exact transport expression")
    return flags[0]


class LocationQueryClassificationTests(unittest.TestCase):
    def test_incidental_queries_do_not_enable_location_on_users_me(self):
        for transport_method in ("request", "request_detailed"):
            for path in (
                "/api/v1/users/me",
                "/api/v1/users/me?note=incidental",
                "/api/v1/users/me?note=quota",
                "/api/v1/users/me?note=eligibility",
                "/api/v1/users/me?note=QUOTA&note=ELIGIBILITY",
                "/api/v1/users/me?quota=eligibility",
                "/api/v1/users/me#quota",
                "/api/v1/users/me#eligibility",
                "/api/v1/users/me?note=incidental#quota",
            ):
                with self.subTest(transport=transport_method, path=path):
                    self.assertFalse(location_boolean(transport_method, path))

    def test_existing_quota_and_eligibility_path_predicate_is_preserved(self):
        # These are public route templates with a placeholder organization,
        # not location samples or a claim about a remote method/alias contract.
        for transport_method in ("request", "request_detailed"):
            for path in (
                "/api/v1/orgs/fixture/quota",
                "/api/v1/orgs/fixture/quota?note=incidental",
                "/api/v1/auth/uber/eligibility",
                "/api/v1/auth/uber/eligibility?note=incidental",
                "/api/v1/orgs/fixture/quota#incidental",
                "/api/v1/auth/uber/eligibility#incidental",
            ):
                with self.subTest(transport=transport_method, path=path):
                    self.assertTrue(location_boolean(transport_method, path))


if __name__ == "__main__":
    unittest.main()
