from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from moneymin.web import server


class PreviewStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = server.create_app().test_client()

    @staticmethod
    def _log(path: Path, accounts: list[dict]) -> Path:
        path.write_text(
            json.dumps({"items": [{"accounts": accounts}]}),
            encoding="utf-8",
        )
        return path

    def test_summary_counts_files_instead_of_sessions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qmoney-preview-status-") as tmp:
            path = self._log(Path(tmp) / "campaign.json", [
                {
                    "email": "user@example.com", "org_key": "org",
                    "session_id": "one", "uploads": ["a", "b", "c"],
                },
                {
                    "email": "user@example.com", "org_key": "org",
                    "session_id": "two", "uploads": ["d", "e"],
                },
            ])
            results = [
                {
                    "session_id": "one", "email": "user@example.com",
                    "status": "processing", "ready_files": 1,
                    "pending_files": 2, "unavailable_files": 0,
                    "total_files": 3,
                },
                {
                    "session_id": "two", "email": "user@example.com",
                    "status": "preview_ready", "ready_files": 2,
                    "pending_files": 0, "unavailable_files": 0,
                    "total_files": 2,
                },
            ]
            with mock.patch.object(server, "_log_path", return_value=path), \
                 mock.patch.object(server.Session, "from_email", return_value=object()), \
                 mock.patch.object(server.campaign, "session_result",
                                   side_effect=results):
                response = self.client.post("/api/logs/campaign.json/status")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["summary"], {
            "total": 5, "ready": 3, "pending": 2,
            "unavailable": 0, "errors": 0, "transient_errors": 0,
        })
        self.assertEqual(payload["sessions"], {
            "total": 2, "ready": 1, "pending": 1,
            "unavailable": 0, "errors": 0,
        })

    def test_transient_session_error_keeps_expected_file_count(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qmoney-preview-error-") as tmp:
            path = self._log(Path(tmp) / "campaign.json", [{
                "email": "user@example.com", "org_key": "org",
                "session_id": "one", "uploads": ["a", "b", "c"],
            }])
            with mock.patch.object(server, "_log_path", return_value=path), \
                 mock.patch.object(server.Session, "from_email", return_value=object()), \
                 mock.patch.object(server.campaign, "session_result",
                                   side_effect=RuntimeError("rede indisponível")):
                response = self.client.post("/api/logs/campaign.json/status")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["summary"], {
            "total": 3, "ready": 0, "pending": 0,
            "unavailable": 0, "errors": 3, "transient_errors": 3,
        })
        self.assertEqual(payload["sessions"]["errors"], 1)

    def test_terminal_preview_error_is_not_marked_retryable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qmoney-preview-terminal-") as tmp:
            path = self._log(Path(tmp) / "campaign.json", [{
                "email": "user@example.com", "org_key": "org",
                "session_id": "one", "uploads": ["a", "b"],
            }])
            result = {
                "session_id": "one", "email": "user@example.com",
                "status": "unprocessed:failed", "ready_files": 0,
                "pending_files": 0, "unavailable_files": 0,
                "total_files": 2,
            }
            with mock.patch.object(server, "_log_path", return_value=path), \
                 mock.patch.object(server.Session, "from_email", return_value=object()), \
                 mock.patch.object(server.campaign, "session_result",
                                   return_value=result):
                response = self.client.post("/api/logs/campaign.json/status")

        summary = response.get_json()["summary"]
        self.assertEqual(summary["errors"], 2)
        self.assertEqual(summary["transient_errors"], 0)

    def test_empty_campaign_never_invents_a_preview(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qmoney-preview-empty-") as tmp:
            path = self._log(Path(tmp) / "campaign.json", [])
            with mock.patch.object(server, "_log_path", return_value=path):
                response = self.client.post("/api/logs/campaign.json/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["summary"]["total"], 0)

    def test_corrupted_log_returns_a_human_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qmoney-preview-corrupt-") as tmp:
            path = Path(tmp) / "campaign.json"
            path.write_text("{", encoding="utf-8")
            with mock.patch.object(server, "_log_path", return_value=path):
                response = self.client.post("/api/logs/campaign.json/status")

        self.assertEqual(response.status_code, 400)
        self.assertIn("log ilegível", response.get_json()["error"])


class CampaignReadinessGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = server.create_app().test_client()
        self.not_ready = {
            "ready": False,
            "checks": [{
                "name": "FFmpeg/FFprobe", "status": "error",
                "detail": "use Reparar instalação na aba Integrações",
            }],
        }

    def test_preflight_exposes_environment_blockers(self) -> None:
        with mock.patch.object(server.readiness, "campaign_readiness",
                               return_value=self.not_ready), \
             mock.patch.object(server, "_list_accounts", return_value=[]):
            response = self.client.post("/api/campaigns/preflight", json={
                "accounts": [], "tasks": [], "dataset": "ego4d",
            })

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload["ok"])
        self.assertTrue(any("FFmpeg/FFprobe" in item
                            for item in payload["blockers"]))

    def test_campaign_endpoint_cannot_bypass_readiness(self) -> None:
        with mock.patch.object(server.readiness, "campaign_readiness",
                               return_value=self.not_ready), \
             mock.patch.object(server, "_list_accounts",
                               return_value=[{"email": "user@example.com"}]):
            response = self.client.post("/api/campaigns", json={
                "accounts": ["user@example.com"],
                "tasks": [{"task_id": "task"}],
                "dataset": "ego4d",
            })

        self.assertEqual(response.status_code, 400)
        self.assertIn("ambiente não está pronto", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
