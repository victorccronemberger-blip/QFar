import copy
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from moneymin.web import server


class IntegrationConcurrencyTests(unittest.TestCase):
    def test_ego4d_and_hostinger_saves_preserve_both_integrations(self):
        stored = {"schema": 2, "ego4d": {}, "hostinger": {"profiles": []}}
        ego_testing = threading.Event()
        host_saved = threading.Event()

        def test_ego(values):
            ego_testing.set()
            host_saved.wait(0.5)
            return {"ok": True}

        def save(path, data):
            stored.clear()
            stored.update(copy.deepcopy(data))

        app = server.create_app()
        def ego_request():
            with app.test_client() as client:
                return client.put("/api/integrations/ego4d", json={
                    "access_key_id": "fixture-access-key", "secret_access_key": "fixture-secret-key-long"}).status_code

        def host_request():
            self.assertTrue(ego_testing.wait(3))
            with app.test_client() as client:
                response = client.put("/api/integrations/hostinger", json={
                    "create": True, "name": "Fixture", "token": "fixture-hostinger-token"})
            host_saved.set()
            return response.status_code

        with patch.object(server, "_migrate_legacy_integrations", side_effect=lambda: copy.deepcopy(stored)), \
             patch.object(server, "save_secure_settings", side_effect=save), \
             patch.object(server, "_test_ego4d", side_effect=test_ego), \
             patch.object(server.hostinger_mail, "test_connection", return_value={"ok": True}), \
             patch.object(server, "_apply_ego4d"), patch.object(server, "_apply_hostinger"), \
             patch.object(server, "_integration_snapshot", return_value={}), \
             ThreadPoolExecutor(2) as pool:
            ego = pool.submit(ego_request)
            host = pool.submit(host_request)
            self.assertEqual(ego.result(timeout=5), 200)
            self.assertEqual(host.result(timeout=5), 200)
        self.assertEqual(stored["ego4d"]["access_key_id"], "fixture-access-key")
        self.assertEqual(stored["hostinger"]["profiles"][0]["name"], "Fixture")
