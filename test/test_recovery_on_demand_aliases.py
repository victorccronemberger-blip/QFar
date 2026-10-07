"""Immutable receipt lineage reserves and reconciles one physical Nymeria cut."""
from __future__ import annotations

import copy
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from moneymin import config, content_provenance, recovery, sent_registry, upload


class RecoveryOnDemandAliasesTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.journals = self.root / "sidecars"
        self.journals.mkdir()
        for entry in (patch.object(config, "DATA_DIR", self.root),
                      patch.object(config, "MEDIA_DATA_DIR", self.root),
                      patch.object(upload, "sidecars_dir", return_value=self.journals),
                      patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))):
            self.stack.enter_context(entry)
        self.email = "fixture@example.invalid"
        self.key = "minute|task|Fixture"
        self.uid = "nymeria:sequence:10.000:310.000"
        self.alias = "nymeria-planned:sequence:1010000000000:1310000000000"

    def lineage(self):
        task = {"name": "Fixture", "id": "task", "registry_key": self.key}
        absolute = [1010000000000, 1310000000000]
        planned_evidence = {"schema": 1, "dataset": "nymeria", "algorithm": "nymeria-atomic-device-v4-planned", "task": task,
            "sensor_coverage": "unmeasured", "planned_device_window_ns": absolute}
        plan = {"clip_uid": self.alias, "exported_clip_uid": self.alias, "seq_id": "sequence",
            "parent_video_uid": "sequence", "source": "nymeria", "acquisition_required": True,
            "source_clock_domain": "aria_DEVICE_TIME_ns", "planned_device_window_ns": absolute,
            "device_window_ns": absolute, "selection_evidence": planned_evidence}
        evidence = {"schema": 1, "dataset": "nymeria", "algorithm": "nymeria-atomic-device-v4",
            "task": task, "window_s": [10.0, 310.0],
            "measured_window_ns": [1000000000000, 1500000000000], "acquisition_plan": plan}
        value = {"schema": 1, "dataset": "nymeria", "clip_uid": self.uid,
            "parent_video_uid": "sequence", "window_s": [10.0, 310.0], "selection_evidence": evidence}
        value["lineage_sha256"] = content_provenance.canonical_digest(value)
        return value

    def binding(self, sid, lineage=None):
        value = {"schema": 1, "content": lineage or self.lineage(), "session_id": sid,
            "task_id": "task", "org_key": "fixture-org", "chunks": [
                {"index": 0, "start_ms": 0, "duration_ms": 150000},
                {"index": 1, "start_ms": 150000, "duration_ms": 150000}],
            "confirmed_delivery": False, "physical_provenance_verified": False}
        value["delivery_binding_sha256"] = content_provenance.canonical_digest(value)
        return value

    def persist(self, sid="fixture-session", *, confirmed=True, binding=None):
        context = {"registry_key": self.key, "clip_uid": self.uid, "task_id": "task",
                   "history_name": "campaign_old.json", "content_provenance": binding or self.binding(sid)}
        rows = []
        for index in range(2):
            row = {"session_id": sid, "account_email": self.email, "org_key": "fixture-org",
                "task_id": "task", "chunk_index": index, "expected_chunk_count": 2,
                "upload_id": f"fixture-upload-{sid}-{index}", "state": "done" if confirmed else "failed",
                "phase": "done" if confirmed else "create", "finalized": confirmed,
                "evaluation_required": False, "create_attempted": True, "campaign_context": copy.deepcopy(context)}
            upload.save_sidecar(row)
            rows.append(row)
        return rows

    def journal_bytes(self):
        return {path.name: path.read_bytes() for path in self.journals.glob("*.json")}

    def test_pending_receipt_reserves_canonical_and_planned_without_source_files(self):
        self.persist(confirmed=False)
        before = self.journal_bytes()
        snapshot = recovery.snapshot()
        self.assertEqual(snapshot["items"][0]["delivery_clip_uids"], [self.uid, self.alias])
        self.assertEqual(recovery.campaign_exclusions(snapshot["items"]),
                         {self.uid: [self.email], self.alias: [self.email]})
        self.assertEqual(recovery.reconcile_confirmed()["reconciled"], 0)
        self.assertEqual(sent_registry.sent_emails(self.key, self.alias), set())
        self.assertEqual(self.journal_bytes(), before)

    def test_confirmed_multipart_receipt_marks_two_identities_but_one_session(self):
        self.persist()
        before = self.journal_bytes()
        result = recovery.reconcile_confirmed()
        self.assertEqual(result["reconciled"], 1)
        self.assertEqual(result["reconciled_sessions"][0]["clip_uid"], self.uid)
        self.assertEqual(sent_registry.sent_emails(self.key, self.uid), {self.email})
        self.assertEqual(sent_registry.sent_emails(self.key, self.alias), {self.email})
        for name, raw in self.journal_bytes().items():
            original, current = json.loads(before[name]), json.loads(raw)
            self.assertEqual(current.pop("campaign_reconciled"), True)
            self.assertEqual(current, original)
        self.assertEqual(recovery.reconcile_confirmed()["reconciled"], 0)

    def test_invalid_outer_hash_inner_hash_or_cross_session_binding_blocks_recovery(self):
        for invalid in ("outer", "inner", "session", "org"):
            with self.subTest(invalid=invalid):
                value = self.binding("fixture-session")
                if invalid == "outer":
                    value["delivery_binding_sha256"] = "0" * 64
                elif invalid == "inner":
                    value["content"]["lineage_sha256"] = "0" * 64
                    value["delivery_binding_sha256"] = content_provenance.canonical_digest(
                        {key: row for key, row in value.items() if key != "delivery_binding_sha256"})
                else:
                    value["session_id" if invalid == "session" else "org_key"] = "other"
                    value["delivery_binding_sha256"] = content_provenance.canonical_digest(
                        {key: row for key, row in value.items() if key != "delivery_binding_sha256"})
                # Test isolated read consumers with original durable identities.
                for path in self.journals.glob("*.json"):
                    path.unlink()
                self.persist(binding=value)
                before = self.journal_bytes()
                item = recovery.snapshot()["items"][0]
                self.assertEqual(item["clip_uid"], self.uid)
                self.assertTrue(item["blocks_campaign"])
                self.assertEqual(item["status"], "needs_review")
                self.assertFalse(item["can_resume"])
                self.assertEqual(recovery.campaign_exclusions([item]), {self.uid: [self.email]})
                self.assertEqual(recovery.reconcile_confirmed()["reconciled"], 0)
                self.assertEqual(self.journal_bytes(), before)

    def test_generic_history_aliases_cannot_add_delivery_or_reservation(self):
        item = {"clip_uid": self.uid, "email": self.email, "dedup_clip_uids": ["foreign-alias"]}
        self.assertEqual(recovery.campaign_exclusions([item]), {self.uid: [self.email]})
        rows = self.persist()
        for row in rows:
            row["campaign_context"].pop("content_provenance")
            row["campaign_context"]["dedup_clip_uids"] = ["foreign-alias"]
        for path in self.journals.glob("*.json"):
            path.unlink()
        for row in rows:
            upload.save_sidecar(row)
        recovery.reconcile_confirmed()
        self.assertEqual(sent_registry.sent_emails(self.key, "foreign-alias"), set())
        self.assertEqual(sent_registry.sent_emails(self.key, self.alias), set())

    def test_scoped_reset_releases_both_confirmed_aliases_and_preserves_receipts(self):
        self.persist()
        history = {"items": [{"clip_uid": self.uid, "task_id": "task", "registry_key": self.key,
            "content_provenance": self.lineage(), "accounts": [{"email": self.email, "org_key": "fixture-org",
            "session_id": "fixture-session", "ok": True, "finalized": True}]}]}
        history_path = self.root / "campaign_old.json"
        history_path.write_text(json.dumps(history), "utf8")
        recovery.reconcile_confirmed()
        before = self.journal_bytes()
        raw_history = history_path.read_bytes()
        sent_registry.reset(history_names=[history_path.name])
        self.assertEqual(sent_registry.sent_emails(self.key, self.uid), set())
        self.assertEqual(sent_registry.sent_emails(self.key, self.alias), set())
        self.assertEqual(recovery.snapshot()["items"], [])
        self.assertEqual(self.journal_bytes(), before)
        self.assertEqual(history_path.read_bytes(), raw_history)

    def test_scoped_reset_preserves_both_aliases_needed_by_pending_session(self):
        self.persist()
        self.persist("fixture-pending", confirmed=False)
        history = {"items": [{"clip_uid": self.uid, "task_id": "task", "registry_key": self.key,
            "content_provenance": self.lineage(), "accounts": [{"email": self.email, "org_key": "fixture-org",
            "session_id": "fixture-session", "ok": True, "finalized": True}]}]}
        history_path = self.root / "campaign_old.json"
        history_path.write_text(json.dumps(history), "utf8")
        recovery.reconcile_confirmed()
        before = self.journal_bytes()
        sent_registry.reset(history_names=[history_path.name])
        self.assertEqual(sent_registry.sent_emails(self.key, self.uid), {self.email})
        self.assertEqual(sent_registry.sent_emails(self.key, self.alias), {self.email})
        items = recovery.snapshot()["items"]
        self.assertTrue(any(row["session_id"] == "fixture-pending" for row in items))
        self.assertEqual(recovery.campaign_exclusions(items),
                         {self.uid: [self.email], self.alias: [self.email]})
        self.assertEqual(self.journal_bytes(), before)


if __name__ == "__main__":
    unittest.main()
