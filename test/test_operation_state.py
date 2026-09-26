import unittest
from moneymin.web.operation_state import OperationState
from moneymin.web.runner import CampaignRunner


class OperationStateTests(unittest.TestCase):
    def test_transfer_progress_is_not_receipt_confirmation(self):
        state = OperationState(["a"])
        state.event("account_progress", dict(email="a", phase="transport", percent=100))
        self.assertEqual(state.snapshot()["accounts"][0]["state"], "sending")
        state.event("account_done", dict(email="a", ok=True, finalized=False))
        self.assertEqual(state.snapshot()["accounts"][0]["state"], "unconfirmed")

    def test_skips_and_failures_are_not_confirmed(self):
        state = OperationState()
        for email, payload in [("a", dict(ok=True, skipped=True)), ("b", dict(ok=False)),
                               ("c", dict(ok=True, finalized=True))]:
            state.event("account_done", dict(email=email, **payload))
        self.assertEqual(state.snapshot()["counts"], dict(skipped=1, failed=1, confirmed=1))

    def test_snapshot_is_detached_and_terminal_keeps_pending_visible(self):
        state = OperationState(["a"])
        state.event("account_progress", dict(email="a", phase="complete"))
        snapshot = state.snapshot(True)
        self.assertEqual(snapshot["accounts"][0]["state"], "pending")
        snapshot["accounts"][0]["email"] = "changed"
        self.assertEqual(state.snapshot()["accounts"][0]["email"], "a")
        self.assertEqual(state.snapshot()["accounts"][0]["state"], "confirming")

    def test_new_clip_resets_progress_but_keeps_delivery_count(self):
        state = OperationState(["a"])
        state.event("account_done", dict(email="a", ok=True, finalized=True, session_id="old"))
        state.event("account_start", dict(email="a", clip_uid="next"))
        row = state.snapshot()["accounts"][0]
        self.assertEqual(row["confirmed"], 1)
        self.assertEqual(row["clip_uid"], "next")
        self.assertIsNone(row["session_id"])

    def test_invalid_progress_and_secret_payloads_are_not_exposed(self):
        state = OperationState()
        for value in [None, "bad", float("nan"), float("inf")]:
            state.event("account_progress", dict(email="a", phase="transport", percent=value, token="SECRET"))
            self.assertIsNone(state.snapshot()["accounts"][0]["progress"])
        self.assertNotIn("SECRET", str(state.snapshot()))

    def test_runner_exposes_state_without_relying_on_event_buffer(self):
        runner = CampaignRunner()
        runner._on_event("account_done", dict(email="a", ok=True, finalized=True, session_id="s"))
        runner.events.clear()
        self.assertEqual(runner.snapshot()["operation"]["accounts"][0]["session_id"], "s")
