"""Local corruption after provider acceptance never discards that outcome."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moneymin.web import server


class PostAcceptanceStateTests(unittest.TestCase):
    def test_corruption_after_submission_preserves_acceptance_without_second_request(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            balance = root / "balances.json"
            balance.write_bytes(b'{}')
            def accepted(*_args, **_kwargs):
                balance.write_bytes(b'{"private-canary":')
                return {"status": "ok"}
            with patch.object(server.config, "DATA_DIR", root), \
                 patch.object(server, "BALANCES_PATH", balance), \
                 patch.object(server, "_WITHDRAW_COOLDOWN_LOADED", False), \
                 patch.object(server, "_WITHDRAW_LAST_REQUEST", {}), \
                 patch.object(server, "_WITHDRAW_IN_FLIGHT", set()), \
                 patch.object(server.crowtado, "solicitar_link_saque", side_effect=accepted) as provider:
                result, status = server._withdraw_once_locked("one@example.invalid", "fixture-password")
            self.assertEqual(status, 200)
            self.assertTrue(result["ok"])
            self.assertEqual(result["result"]["status"], "ok")
            self.assertIn("não repita este saque", result["message"])
            self.assertNotIn("private-canary", str(result))
            provider.assert_called_once()
            self.assertEqual(balance.read_bytes(), b'{"private-canary":')


if __name__ == "__main__":
    unittest.main()
