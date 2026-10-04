"""Clock rollback preserves unresolved withdrawal attempts before provider calls."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from moneymin.web import server


class WithdrawClockIntegrityTests(unittest.TestCase):
    EMAIL = "clock-owner@example.invalid"
    OTHER = "other-owner@example.invalid"

    def scenario(self, saved, now, *, loaded=False, memory=None):
        """Only temporary state and an inert provider boundary are exercised."""
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            root = Path(folder)
            cooldown = root / "withdraw_request_times.json"
            raw = json.dumps(saved, separators=(",", ":")).encode("utf-8")
            cooldown.write_bytes(raw)
            stack.enter_context(patch.object(server.config, "DATA_DIR", root))
            stack.enter_context(patch.object(server.time, "time", return_value=now))
            stack.enter_context(patch.object(server, "_WITHDRAW_COOLDOWN_LOADED", loaded))
            stack.enter_context(patch.object(server, "_WITHDRAW_LAST_REQUEST", dict(memory or {})))
            stack.enter_context(patch.object(server, "_WITHDRAW_IN_FLIGHT", set()))
            stack.enter_context(patch.object(server, "_load_balances", return_value={}))
            stack.enter_context(patch.object(server, "_invalidate_balance_after_withdrawal"))
            stack.enter_context(patch.object(server, "_withdraw_message", return_value="fixture result"))
            provider = stack.enter_context(patch.object(server.crowtado, "solicitar_link_saque", return_value={"status": "unknown"}))
            response, status = server._withdraw_once_locked(self.EMAIL, "fixture-password-unused")
            return {
                "response": response, "status": status, "calls": provider.call_count,
                "before": raw, "after": cooldown.read_bytes(),
                "memory": dict(server._WITHDRAW_LAST_REQUEST),
                "in_flight": set(server._WITHDRAW_IN_FLIGHT),
            }

    def assert_clock_conflict(self, result, timestamp):
        self.assertEqual(result["calls"], 0)
        self.assertEqual(result["status"], 409)
        self.assertFalse(result["response"]["ok"])
        self.assertEqual(result["response"]["code"], "clock_conflict")
        self.assertEqual(result["before"], result["after"])
        self.assertEqual(result["memory"].get(self.EMAIL), timestamp)
        self.assertFalse(result["in_flight"])
        self.assertNotIn("fixture-password-unused", str(result["response"]))
        self.assertNotIn(str(timestamp), str(result["response"]))

    def test_restart_future_selected_preserves_disk_and_vetoes_request(self):
        self.assert_clock_conflict(self.scenario({self.EMAIL: 1000.0}, 999.0), 1000.0)

    def test_loaded_future_selected_preserves_memory_and_vetoes_request(self):
        self.assert_clock_conflict(self.scenario({self.EMAIL: 1000.0}, 999.0,
            loaded=True, memory={self.EMAIL: 1000.0}), 1000.0)

    def test_loaded_consulted_future_selected_overrides_missing_memory(self):
        self.assert_clock_conflict(self.scenario({self.EMAIL: 1000.0}, 999.0,
            loaded=True), 1000.0)

    def test_loaded_consulted_newer_future_other_owner_is_preserved(self):
        result = self.scenario({self.OTHER: 1002.0}, 999.0,
            loaded=True, memory={self.OTHER: 998.0})
        self.assertEqual(result["calls"], 1)
        self.assertEqual(json.loads(result["after"]), {self.OTHER: 1002.0, self.EMAIL: 999.0})
        self.assertFalse(result["in_flight"])

    def test_restart_future_other_owner_survives_new_request_publication(self):
        result = self.scenario({self.OTHER: 1000.0}, 999.0)
        self.assertEqual(result["calls"], 1)
        self.assertEqual(result["status"], 409)
        self.assertEqual(result["response"]["result"]["status"], "unknown")
        self.assertEqual(json.loads(result["after"]), {self.OTHER: 1000.0, self.EMAIL: 999.0})
        self.assertFalse(result["in_flight"])

    def test_loaded_future_other_owner_survives_new_request_publication(self):
        result = self.scenario({self.OTHER: 1000.0}, 999.0,
            loaded=True, memory={self.OTHER: 1000.0})
        self.assertEqual(result["calls"], 1)
        self.assertEqual(json.loads(result["after"]), {self.OTHER: 1000.0, self.EMAIL: 999.0})
        self.assertFalse(result["in_flight"])

    def test_equal_and_recent_attempts_still_throttle_without_publication(self):
        for loaded in (False, True):
            for now in (1000.0, 1001.0, 1000.0 + server._WITHDRAW_COOLDOWN_S - 0.01):
                with self.subTest(loaded=loaded, now=now):
                    result = self.scenario({self.EMAIL: 1000.0}, now,
                        loaded=loaded, memory={self.EMAIL: 1000.0} if loaded else None)
                    self.assertEqual(result["calls"], 0)
                    self.assertEqual(result["status"], 429)
                    self.assertEqual(result["before"], result["after"])

    def test_expired_and_fresh_attempts_keep_existing_request_semantics(self):
        for saved, now in (({}, 999.0), ({self.EMAIL: 1000.0}, 1000.0 + server._WITHDRAW_COOLDOWN_S),
                           ({self.EMAIL: 1000.0}, 1000.0 + server._WITHDRAW_COOLDOWN_S + 1)):
            with self.subTest(saved=saved, now=now):
                result = self.scenario(saved, now)
                self.assertEqual(result["calls"], 1)
                self.assertEqual(result["response"]["result"]["status"], "unknown")
                self.assertEqual(json.loads(result["after"]), {self.EMAIL: now})
                self.assertFalse(result["in_flight"])


if __name__ == "__main__":
    unittest.main()
