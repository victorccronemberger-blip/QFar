"""Each cleanup observes one immutable publication snapshot for all receipts."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from moneymin import campaign_evidence, config, content_provenance, recovery, upload


class RecoveryProtectionSnapshotTests(unittest.TestCase):
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
        self.paths = {}
        for index in range(20):
            self.persist(index, confirmed=True)
        self.persist(20, confirmed=False)

    def persist(self, index, *, confirmed):
        sid, uid = f"fixture-session-{index}", f"fixture-clip-{index}"
        media = self.root / f"fixture-media-{index}.mp4"
        media.write_bytes(f"inert fixture {index}".encode())
        digest = hashlib.sha256(media.read_bytes()).hexdigest()
        content = {"assets": {"source_video": {"sha256": digest}}}
        binding = {"content": content}
        binding["delivery_binding_sha256"] = content_provenance.canonical_digest(binding)
        context = {"registry_key": "minute|task|Fixture", "clip_uid": uid, "task_id": "task",
                   "history_name": f"campaign_fixture_{index}.json", "content_provenance": binding}
        row = {"session_id": sid, "account_email": "fixture@example.invalid", "org_key": "fixture-org",
            "task_id": "task", "chunk_index": 0, "expected_chunk_count": 1,
            "upload_id": f"fixture-upload-{index}", "state": "done" if confirmed else "failed",
            "phase": "done" if confirmed else "create", "finalized": confirmed,
            "evaluation_required": False, "create_attempted": True, "campaign_context": context,
            "video_path": str(media), "campaign_reconciled": confirmed}
        upload.save_sidecar(row)
        history = {"items": [{"clip_uid": uid, "task_id": "task", "registry_key": context["registry_key"],
            "accounts": [{"email": row["account_email"], "org_key": row["org_key"], "session_id": sid,
                          "ok": confirmed, "finalized": confirmed}]}]}
        (self.root / context["history_name"]).write_text(json.dumps(history), "utf8")
        self.paths[index] = (media, digest, upload._sidecar_archive_path(sid, 0).resolve())

    def test_many_confirmed_and_pending_groups_share_one_real_publication_snapshot(self):
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with patch.object(campaign_evidence, "publication_index", wraps=campaign_evidence.publication_index) as read:
            result = recovery.media_cleanup_protection()
        read.assert_called_once()
        pending, digest, archive = self.paths[20]
        self.assertEqual(result, {"paths": {pending.resolve(), archive}, "sha256": {digest}})
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_corrupt_or_missing_publication_snapshot_keeps_every_receipt_protected(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                if corrupt:
                    (self.root / "campaign_fixture_0.json").write_text("{broken", "utf8")
                with patch.object(campaign_evidence, "publication_index",
                                  wraps=campaign_evidence.publication_index) as read:
                    if not corrupt:
                        for index in range(20):
                            (self.root / f"campaign_fixture_{index}.json").unlink()
                    result = recovery.media_cleanup_protection()
                read.assert_called_once()
                self.assertEqual(result["sha256"], {digest for _, digest, _ in self.paths.values()})
                self.assertEqual(result["paths"], {value for media, _, archive in self.paths.values()
                                                  for value in (media.resolve(), archive)})


if __name__ == "__main__":
    unittest.main()
