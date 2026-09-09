from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from moneymin import config, sent_registry
from moneymin.web import server


class SentResetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="qmoney-sent-reset-")
        self.data_dir = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_reset_preserves_history_and_clears_only_sent_registry(self):
        history = self.data_dir / "campaign_20260909_120000.json"
        history.write_text('{"items": []}', encoding="utf-8")
        with mock.patch.object(config, "DATA_DIR", self.data_dir):
            sent_registry.mark_sent("minute|task|Jardinagem", "clip-1", "user@example.com")
            runner = mock.Mock(running=False)
            with mock.patch.object(server, "RUNNER", runner):
                response = server.create_app().test_client().post("/api/sent/reset", json={})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["sent"], [])
            self.assertTrue(history.exists())
            self.assertEqual(sent_registry.load(), {})

    def test_reset_is_blocked_while_campaign_is_stopping(self):
        with mock.patch.object(config, "DATA_DIR", self.data_dir):
            sent_registry.mark_sent("scenario", "clip-1", "user@example.com")
            runner = mock.Mock(running=True)
            with mock.patch.object(server, "RUNNER", runner):
                response = server.create_app().test_client().post("/api/sent/reset", json={})
            self.assertEqual(response.status_code, 409)
            self.assertIn("aguarde a campanha terminar", response.get_json()["error"])
            self.assertEqual(len(sent_registry.summary()), 1)


if __name__ == "__main__":
    unittest.main()
