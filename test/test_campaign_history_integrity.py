import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moneymin import config
from moneymin.web import server


class CampaignHistoryIntegrityTests(unittest.TestCase):
    def test_bad_history_does_not_hide_valid_history_or_trigger_remote_checks(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "DATA_DIR", Path(directory)):
            root = Path(directory)
            (root / "campaign_good.json").write_text(json.dumps({"items": [], "accounts": [], "status": "done"}))
            bad = root / "campaign_bad.json"
            client = server.create_app().test_client()
            values = [[], {"items": None}, {"items": [None]},
                      {"items": [{"accounts": [None]}]},
                      {"items": [{"duration_ms": "nan"}]}, {"accounts": None}, {"issues": None}]
            with patch.object(server.Session, "from_email") as auth:
                for value in values:
                    with self.subTest(value=value):
                        bad.write_text(json.dumps(value))
                        response = client.get("/api/logs")
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual([item["name"] for item in response.get_json()["logs"]], ["campaign_good.json"])
                        self.assertEqual(client.get("/api/logs/campaign_bad.json").status_code, 400)
                        self.assertEqual(client.post("/api/logs/campaign_bad.json/status").status_code, 400)
                auth.assert_not_called()

    def test_bad_sent_registry_returns_actionable_error_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "DATA_DIR", Path(directory)):
            path = Path(directory) / "sent_videos.json"
            path.write_bytes(b"broken")
            response = server.create_app().test_client().get("/api/sent")
            self.assertEqual(response.status_code, 409)
            self.assertIn("Registro de envios", response.get_json()["error"])
            self.assertEqual(path.read_bytes(), b"broken")
