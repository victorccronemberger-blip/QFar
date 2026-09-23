from __future__ import annotations

import datetime
import unittest
from unittest.mock import patch

from moneymin.web import server


class BalanceRefreshNeededTests(unittest.TestCase):
    def test_only_connected_crowtado_with_missing_failed_or_old_balance(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        recent = (now - datetime.timedelta(hours=1)).isoformat()
        old = (now - datetime.timedelta(days=2)).isoformat()
        accounts = [{"email": email} for email in (
            "recent@example.com", "old@example.com", "missing@example.com",
            "failed@example.com", "claru@example.com", "unconnected@example.com")]
        balances = {
            "recent@example.com": {"availableCents": 0, "updated_at": recent},
            "old@example.com": {"availableCents": 500, "updated_at": old},
            "failed@example.com": {"availableCents": 100, "updated_at": recent,
                                   "error": "consulta falhou"},
            "claru@example.com": {"availableCents": 100, "updated_at": old},
            "unconnected@example.com": {"availableCents": 100, "updated_at": old},
        }
        connected = {account["email"] for account in accounts} - {"unconnected@example.com"}
        with patch.object(server.org_policy, "account_kind",
                          side_effect=lambda email: "claru" if email.startswith("claru") else "crowtado"):
            needed = server._balance_refresh_needed(accounts, balances, connected)
        self.assertEqual(needed, ["failed@example.com", "missing@example.com", "old@example.com"])

    def test_invalid_timestamp_is_pending(self):
        with patch.object(server.org_policy, "account_kind", return_value="crowtado"):
            needed = server._balance_refresh_needed(
                [{"email": "a@example.com"}],
                {"a@example.com": {"availableCents": 0, "updated_at": "invalid"}},
                {"a@example.com"})
        self.assertEqual(needed, ["a@example.com"])

    def test_invalid_amount_is_pending(self):
        with patch.object(server.org_policy, "account_kind", return_value="crowtado"):
            needed = server._balance_refresh_needed(
                [{"email": "a@example.com"}, {"email": "b@example.com"}],
                {"a@example.com": {"availableCents": float("nan"),
                                   "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()},
                 "b@example.com": {"availableCents": True,
                                   "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}},
                {"a@example.com", "b@example.com"})
        self.assertEqual(needed, ["a@example.com", "b@example.com"])
