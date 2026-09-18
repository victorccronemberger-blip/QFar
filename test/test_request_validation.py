import unittest
from unittest.mock import patch

from moneymin.web import server


class RequestValidationTests(unittest.TestCase):
    def test_invalid_json_cannot_reset_sent_history(self):
        client = server.create_app().test_client()
        for body, content_type in (("{broken", "application/json"), ("null", "application/json"),
                                   ("[]", "application/json"), ("{}", "text/plain")):
            with self.subTest(body=body, content_type=content_type), \
                 patch.object(server.sent_registry, "reset") as reset:
                response = client.post("/api/sent/reset", data=body, content_type=content_type)
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.is_json)
                reset.assert_not_called()

    def test_all_json_mutation_routes_reject_non_object_bodies(self):
        app = server.create_app()
        client = app.test_client()
        for rule in app.url_map.iter_rules():
            if rule.arguments or not rule.rule.startswith("/api/"):
                continue
            for method in rule.methods & {"POST", "PUT", "PATCH"}:
                with self.subTest(path=rule.rule, method=method):
                    response = client.open(rule.rule, method=method, json=["invalid"])
                    self.assertEqual(response.status_code, 400)
                    self.assertTrue(response.is_json)
