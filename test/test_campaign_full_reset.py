"""Explicit reset uses isolated local files; no accounts or provider network."""
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from moneymin import campaign_reset, campaign_start_store, config, recovery, sent_registry
from moneymin.campaign_types import CampaignLog
from moneymin.web import server
from moneymin.web.runner import CampaignRunner


class CampaignFullResetTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temporary = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="qmoney-full-reset-"))
        self.root = Path(temporary)
        self.data = self.root / "installation" / "data"
        self.media = self.root / "library" / "data"
        self.secrets = self.root / "installation" / "secrets"
        for directory in (self.data, self.media, self.secrets):
            directory.mkdir(parents=True)
        for name, value in (("DATA_DIR", self.data), ("MEDIA_DATA_DIR", self.media),
                            ("SECRETS_DIR", self.secrets)):
            self.stack.enter_context(patch.object(config, name, value))
        self.runner = CampaignRunner()
        self.recoverer = recovery.RecoveryRunner()
        self.stack.enter_context(patch.object(server, "RUNNER", self.runner))
        self.stack.enter_context(patch.object(server, "RECOVERY", self.recoverer))
        self.stack.enter_context(patch.object(server, "HOLO_CACHE_RUNNER", SimpleNamespace(running=False, _thread=None)))
        self.app = server.create_app(for_testing=True)
        self.client = self.app.test_client()
        # A reset must not authenticate or perform a Minute request.
        self.auth = self.stack.enter_context(patch.object(server.Session, "from_email", side_effect=AssertionError("network forbidden")))

    def write(self, root, name, payload=b"old local receipt"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def state_files(self):
        paths = []
        for root in (self.data, self.media):
            for name in ("campaign_20261001_uuid.json", "campaign_start_requests.json",
                         "sent_videos.json", "sent_reset_history.json", "sidecar_migration.json",
                         "start_requests.json", "start_requests.lock", "recording_timeline.json",
                         "original_capture_reservations.json", "task_rank_cache.pkl",
                         "task_rank_cache_hash.pkl.backup-before-legacy", ".campaign_old.json.12.tmp",
                         "sidecars/old-session.json", "sidecars/old-session.data.zip",
                         "sidecars/old-session.write.lock", "upload-session-leases/old.lock"):
                paths.append(self.write(root, name, b"{corrupt historical state"))
            for prefix in ("campaign-history-before-reset-", "retained-confirmed-before-reset-"):
                paths.append(self.write(root.parent / "backups", prefix + "20261006/nested/old.json"))
        return paths

    def remember_campaign(self):
        self.runner.state = "partial"
        self.runner.start_request_id = "old-start"
        self.runner.log_path = "campaign_old.json"
        self.runner._record("campaign_start", accounts=["old-account"], tasks=["task"])
        self.runner.account_seconds = {"old-account": 3600}
        self.runner.credited_deliveries = {("task", "clip", "old-account")}
        self.runner._reported_outcomes = {(("task", "clip", "old-account"), "confirmed")}
        self.runner.target_seconds_per_account = 3600
        self.runner.ok_sends = self.runner.total_sends = self.runner.done_sends = 1
        self.recoverer._state = {"state": "pending", "email": "old-account", "result": {"sessions": ["old-session"]}}

    def test_erases_all_sources_and_backups_without_changing_accounts_or_media(self):
        erased = self.state_files()
        retained = [self.write(self.secrets, "token_account.json", b"private-token"),
                    self.write(self.data, "device_state/account.json", b"Android16 SDK36 profile"),
                    self.write(self.data, "account_health.json"), self.write(self.data, "balances.json"),
                    self.write(self.data, "campaign.example.json"), self.write(self.data, "webui_prefs.json"),
                    self.write(self.data.parent / "backups", "account-backup/identity.json"),
                    self.write(self.media, "ego4d/video.mp4", b"referenced-video"),
                    self.write(self.media, "campaign_demo.mp4", b"library-video"),
                    self.write(self.media, "campaign_demo.mp4.source.json", b"original-source-binding"),
                    self.write(self.media, "nymeria/head.vrs", b"real-sensors"),
                    self.write(self.media, "original_capture/frames.csv", b"measured-pts")]
        original = {path: path.read_bytes() for path in retained}
        size = sum(path.stat().st_size for path in erased)
        self.remember_campaign()
        response = self.client.post("/api/campaign/reset", json={})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json(), {"ok": True, "state": "idle", "removed_files": len(erased), "removed_bytes": size})
        self.assertFalse(any(path.exists() for path in erased))
        self.assertEqual({path: path.read_bytes() for path in retained}, original)
        self.assertFalse(campaign_reset.pending())
        current = self.client.get("/api/campaigns/current").get_json()
        self.assertEqual(current["state"], "idle")
        self.assertEqual(current["events"], [])
        self.assertEqual(current["last_seq"], 0)
        self.assertIsNone(current["start_request_id"])
        self.assertEqual(current["totals"]["total_sends"], 0)
        self.assertEqual(current["operation"]["accounts"], [])
        self.assertEqual(self.recoverer.snapshot(), {"state": "idle", "email": None, "error": None})
        self.auth.assert_not_called()

    def test_fresh_service_does_not_seed_or_import_old_campaign(self):
        self.state_files()
        self.assertEqual(self.client.post("/api/campaign/reset", json={}).status_code, 200)
        restarted = server.create_app(for_testing=True).test_client()
        self.assertEqual(restarted.get("/api/logs").get_json(), {"logs": []})
        self.assertEqual(restarted.get("/api/sent").get_json(), {"sent": []})
        self.assertEqual(recovery.snapshot()["items"], [])
        self.assertFalse(campaign_start_store.public_status("old-start")["found"])
        self.assertEqual(sent_registry.load(), {})
        self.assertEqual(list((self.data / "sidecars").iterdir()), [])
        self.auth.assert_not_called()

    def test_custom_dotted_history_is_erased_and_media_markers_cannot_reseed(self):
        dotted = self.data / "campaign_local.manual.json"
        log = CampaignLog(started_at="2026-10-06T12:00:00Z", accounts=["fixture@example.invalid"], status="done")
        log.add_item({"clip_uid": "old-clip", "task_id": "task", "task_name": "Task",
                      "registry_key": "minute|task|Task",
                      "accounts": [{"email": "fixture@example.invalid", "ok": True, "finalized": True}]})
        log.save(dotted)
        marker = self.write(self.data, "campaign_recording.mp4.source.json", b"damaged-source-marker")
        self.assertEqual(len(sent_registry.load(persist_seed=False)), 1)
        response = self.client.post("/api/campaign/reset", json={})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertFalse(dotted.exists())
        self.assertEqual(marker.read_bytes(), b"damaged-source-marker")
        self.assertEqual(sent_registry.load(persist_seed=False), {})
        self.assertEqual(self.client.get("/api/logs").get_json(), {"logs": []})
        self.assertEqual(recovery.snapshot()["items"], [])

    def test_move_failure_rolls_back_every_selected_file_and_preserves_memory(self):
        files = self.state_files()
        before = {path: path.read_bytes() for path in files}
        self.remember_campaign()
        rename = Path.rename
        moves = 0
        def failed_move(path, destination):
            nonlocal moves
            if Path(destination).parent.name == ".campaign-reset-pending":
                moves += 1
                if moves == 2:
                    raise PermissionError("inert lock")
            return rename(path, destination)
        with patch.object(Path, "rename", failed_move):
            response = self.client.post("/api/campaign/reset", json={})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error_code"], "campaign_reset_move")
        self.assertFalse(response.get_json()["partial"])
        self.assertEqual({path: path.read_bytes() for path in files}, before)
        self.assertEqual(self.runner.start_request_id, "old-start")
        self.assertFalse(campaign_reset.pending())

    def test_failed_purge_blocks_restart_and_retry_removes_staged_bytes(self):
        self.state_files()
        self.remember_campaign()
        with patch.object(campaign_reset, "_remove_tree", side_effect=PermissionError("inert file lock")):
            response = self.client.post("/api/campaign/reset", json={})
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.get_json()["partial"])
        self.assertTrue(campaign_reset.pending())
        restarted = server.create_app(for_testing=True).test_client()
        blocked = restarted.post("/api/campaigns/preflight", json={})
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.get_json()["error_code"], "campaign_reset_incomplete")
        self.assertEqual(restarted.post("/api/campaign/reset", json={}).status_code, 200)
        self.assertFalse(campaign_reset.pending())
        self.assertFalse(campaign_reset._targets())
        self.assertEqual(self.runner.state, "idle")
        self.assertEqual(restarted.post("/api/campaign/reset", json={}).status_code, 200)

    def test_alive_workers_even_with_terminal_labels_cannot_be_reset(self):
        original = self.write(self.data, "campaign_old.json")
        release = threading.Event()
        thread = threading.Thread(target=lambda: release.wait(5))
        thread.start()
        try:
            for worker in (self.runner, self.recoverer):
                worker._thread = thread
                response = self.client.post("/api/campaign/reset", json={})
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.get_json()["error_code"], "campaign_reset_busy")
                self.assertTrue(original.exists())
                worker._thread = None
        finally:
            release.set()
            thread.join(5)

    def test_retry_move_failure_keeps_previous_barrier_and_bytes(self):
        old = self.write(self.data, ".campaign-reset-pending/prior-session.json")
        current = self.write(self.data, "campaign_current.json")
        before = {path: path.read_bytes() for path in (old, current)}
        rename = Path.rename
        def fail_current(path, target):
            if path == current:
                raise PermissionError("inert retry failure")
            return rename(path, target)
        with patch.object(Path, "rename", fail_current):
            response = self.client.post("/api/campaign/reset", json={})
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.get_json()["partial"])
        self.assertTrue(campaign_reset.pending())
        self.assertEqual({path: path.read_bytes() for path in (old, current)}, before)
        self.assertEqual(self.client.post("/api/campaign/reset", json={}).status_code, 200)
        self.assertFalse(campaign_reset.pending())
        self.assertFalse(old.exists() or current.exists())

    def test_old_backup_is_removed_even_if_its_data_directory_was_deleted(self):
        self.media.rmdir()
        archived = self.write(self.media.parent / "backups", "campaign-history-before-reset-old/old.json")
        prior = self.write(self.media.parent / "backups", ".campaign-reset-pending/prior.json")
        self.assertTrue(campaign_reset.pending())
        self.assertEqual(self.client.post("/api/campaign/reset", json={}).status_code, 200)
        self.assertFalse(archived.exists() or prior.exists())
        self.assertFalse(campaign_reset.pending())

    def test_scope_paths_and_invalid_json_cannot_widen_reset(self):
        original = self.write(self.data, "campaign_old.json")
        for body in ({"path": str(self.root)}, {"scenario": "old"}, {"confirmed": True}):
            self.assertEqual(self.client.post("/api/campaign/reset", json=body).status_code, 400)
        for raw in (b"[]", b"null", b"{broken"):
            self.assertEqual(self.client.post("/api/campaign/reset", data=raw, content_type="application/json").status_code, 400)
        self.assertTrue(original.exists())

    def test_symlink_is_rejected_before_erasing_anything(self):
        original = self.write(self.data, "campaign_old.json")
        outside = self.root / "unrelated"
        outside.mkdir()
        precious = self.write(outside, "account.json")
        try:
            (self.media / "sidecars").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Host cannot create symlinks")
        response = self.client.post("/api/campaign/reset", json={})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error_code"], "campaign_reset_scope")
        self.assertTrue(original.exists())
        self.assertTrue(precious.exists())

    def test_library_nested_inside_state_folder_is_never_deleted(self):
        original = self.write(self.data, "campaign_old.json")
        nested = self.data / "sidecars" / "library" / "data"
        video = self.write(nested, "ego4d/video.mp4", b"protected-library")
        with patch.object(config, "MEDIA_DATA_DIR", nested):
            response = self.client.post("/api/campaign/reset", json={})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error_code"], "campaign_reset_protected_scope")
        self.assertEqual(video.read_bytes(), b"protected-library")
        self.assertTrue(original.exists())

    def test_credentials_nested_inside_state_folder_are_never_deleted(self):
        nested = self.data / "sidecars" / "credentials"
        token = self.write(nested, "token.json", b"protected-access")
        with patch.object(config, "SECRETS_DIR", nested):
            response = self.client.post("/api/campaign/reset", json={})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error_code"], "campaign_reset_protected_scope")
        self.assertEqual(token.read_bytes(), b"protected-access")


if __name__ == "__main__":
    unittest.main()
