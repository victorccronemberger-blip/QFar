"""Real OS barriers and isolated state writers; no accounts or remote calls."""
from contextlib import ExitStack
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from moneymin import campaign, campaign_start_store, config, original_capture, recovery
from moneymin import recording_timeline, sent_registry, upload, upload_storage
from moneymin.campaign_state import CampaignStateLeaseError, campaign_state_lease
from moneymin.campaign_types import CampaignLog
from moneymin.upload_protocol_lease import pending_protocol, session_protocol


class CampaignStateLeaseTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temporary = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="qmoney-campaign-barrier-"))
        self.root = Path(temporary)
        self.data = self.root / "state" / "data"
        self.media = self.root / "library" / "data"
        self.secrets = self.root / "state" / "secrets"
        for directory in (self.data, self.media, self.secrets):
            directory.mkdir(parents=True)
        self.stack.enter_context(patch.multiple(config, DATA_DIR=self.data,
            MEDIA_DATA_DIR=self.media, SECRETS_DIR=self.secrets))

    def hold_thread(self, *, exclusive=False, fn=None):
        claimed, release = threading.Event(), threading.Event()
        errors = []

        def hold():
            try:
                if fn is not None:
                    fn(claimed, release)
                else:
                    with campaign_state_lease(exclusive=exclusive):
                        claimed.set()
                        if not release.wait(10):
                            raise AssertionError("fixture release timed out")
            except BaseException as exc:
                errors.append(exc)
                claimed.set()

        thread = threading.Thread(target=hold)
        thread.start()

        def finish():
            release.set()
            thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

        self.addCleanup(finish)
        self.assertTrue(claimed.wait(10))
        self.assertEqual(errors, [])
        return release, thread

    def child(self, *, exclusive=False, wait=False):
        code = '''
import os, sys
from pathlib import Path
os.environ['QMONEY_USER_ROOT'] = str(Path(sys.argv[1]).parent)
os.environ['QMONEY_LIBRARY_ROOT'] = str(Path(sys.argv[2]).parent)
from moneymin import config
from moneymin.campaign_state import CampaignStateLeaseError, campaign_state_lease
try:
    with campaign_state_lease(exclusive=sys.argv[3] == 'exclusive'):
        print('claimed', flush=True)
        if sys.argv[4] == 'wait':
            sys.stdin.readline()
except CampaignStateLeaseError as exc:
    print(exc.code, flush=True)
    sys.exit(4)
'''
        process = subprocess.Popen([sys.executable, "-u", "-c", code, str(self.data),
            str(self.media), "exclusive" if exclusive else "shared", "wait" if wait else "exit"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=Path(__file__).resolve().parents[1],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

        def finish():
            if process.poll() is None:
                try:
                    process.communicate("release\n", timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=10)
            else:
                process.communicate(timeout=10)

        self.addCleanup(finish)
        return process

    def claimed(self, process):
        lines = queue.Queue()
        reader = threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True)
        reader.start()
        try:
            self.assertEqual(lines.get(timeout=10).strip(), "claimed")
        finally:
            if process.poll() is not None:
                reader.join(1)

    def row(self, sid="inert-session"):
        return dict(session_id=sid, chunk_index=0, expected_chunk_count=1,
            account_email="local@example.invalid", org_key="local-org", task_id="local-task",
            state="failed", phase="create", create_attempted=True,
            campaign_context={"registry_key": "minute|local-task", "clip_uid": "local-clip"})

    def test_threads_share_while_reset_is_nonblocking(self):
        release, thread = self.hold_thread()
        with campaign_state_lease():
            with self.assertRaises(CampaignStateLeaseError) as caught:
                with campaign_state_lease(exclusive=True):
                    self.fail("shared upgrade must be rejected")
            self.assertEqual(caught.exception.code, "campaign_reset_busy")
        with self.assertRaises(CampaignStateLeaseError):
            with campaign_state_lease(exclusive=True):
                self.fail("another thread still owns the shared barrier")
        release.set()
        thread.join(10)
        with campaign_state_lease(exclusive=True):
            pass

    def test_exclusive_reentrant_reads_and_shared_nesting_do_not_release_outer_lock(self):
        with campaign_state_lease(exclusive=True):
            with campaign_state_lease(), campaign_state_lease(exclusive=True):
                self.assertEqual(sent_registry.load(), {})
            blocked = self.child()
            out, err = blocked.communicate(timeout=10)
            self.assertEqual(blocked.returncode, 4, err)
            self.assertEqual(out.strip(), "campaign_reset_busy")
        with campaign_state_lease():
            with campaign_state_lease():
                pass
            blocked = self.child(exclusive=True)
            out, err = blocked.communicate(timeout=10)
            self.assertEqual(blocked.returncode, 4, err)

    def test_two_processes_share_and_exclusive_conflicts_with_both(self):
        with campaign_state_lease():
            shared = self.child(wait=True)
            self.claimed(shared)
            blocked = self.child(exclusive=True)
            out, err = blocked.communicate(timeout=10)
            self.assertEqual(blocked.returncode, 4, err)
            self.assertEqual(out.strip(), "campaign_reset_busy")
        # The child continues to own its shared lease after this thread exits.
        with self.assertRaises(CampaignStateLeaseError):
            with campaign_state_lease(exclusive=True):
                self.fail("reset must see the other process")
        out, err = shared.communicate("release\n", timeout=10)
        self.assertEqual(shared.returncode, 0, err)
        with campaign_state_lease(exclusive=True):
            pass

    def test_exclusive_process_blocks_writer_before_journal_and_migration(self):
        legacy = self.media / "sidecars" / "legacy.json"
        legacy.parent.mkdir()
        legacy.write_bytes(b"inert legacy receipt")
        holder = self.child(exclusive=True, wait=True)
        self.claimed(holder)
        with self.assertRaises(CampaignStateLeaseError):
            upload.save_sidecar(self.row())
        with self.assertRaises(CampaignStateLeaseError):
            upload.list_sidecars()
        with self.assertRaises(CampaignStateLeaseError):
            upload_storage.journal_directory()
        self.assertEqual(legacy.read_bytes(), b"inert legacy receipt")
        self.assertFalse((self.data / "sidecars").exists())
        self.assertFalse((self.data / "sidecar_migration.json").exists())
        out, err = holder.communicate("release\n", timeout=10)
        self.assertEqual(holder.returncode, 0, err)

    def test_crashed_process_releases_lock_without_removing_neutral_file(self):
        holder = self.child(wait=True)
        self.claimed(holder)
        marker = self.data / ".campaign-state.lock"
        before = marker.read_bytes()
        holder.kill()
        holder.communicate(timeout=10)
        with campaign_state_lease(exclusive=True):
            self.assertTrue(marker.exists())
        self.assertEqual(marker.read_bytes(), before)
        self.assertTrue(marker.exists())

    def test_unrelated_root_and_body_exception_release_are_independent(self):
        release, thread = self.hold_thread(exclusive=True)
        with campaign_state_lease(exclusive=True, root=self.root / "other"):
            pass
        release.set()
        thread.join(10)
        with self.assertRaisesRegex(RuntimeError, "inert body failure"):
            with campaign_state_lease():
                raise RuntimeError("inert body failure")
        with campaign_state_lease(exclusive=True):
            pass

    def test_session_protocol_parallel_accounts_hold_shared_for_entire_body(self):
        @session_protocol
        def inert_send(claimed, release, *, session_id=None):
            claimed.set()
            if not release.wait(10):
                raise AssertionError("fixture release timed out")
            return session_id

        releases = []
        threads = []
        for sid in ("account-one-session", "account-two-session"):
            release, thread = self.hold_thread(fn=lambda claimed, done, sid=sid:
                inert_send(claimed, done, session_id=sid))
            releases.append(release)
            threads.append(thread)
        with self.assertRaises(CampaignStateLeaseError):
            with campaign_state_lease(exclusive=True):
                self.fail("protocol effects still active")
        releases[0].set()
        threads[0].join(10)
        with self.assertRaises(CampaignStateLeaseError):
            with campaign_state_lease(exclusive=True):
                self.fail("second account still active")
        releases[1].set()
        threads[1].join(10)
        with campaign_state_lease(exclusive=True):
            pass
        self.assertFalse((self.data / "sidecars").exists())

    def test_startup_catalog_holds_shared_until_derived_cache_publication_finishes(self):
        cache = self.data / "task_rank_cache.pkl"
        claimed, release = threading.Event(), threading.Event()

        def build_catalog():
            claimed.set()
            if not release.wait(10):
                raise AssertionError("fixture release timed out")
            cache.write_bytes(b"inert derived catalog")

        with patch.object(campaign, "_ranked_pools", side_effect=build_catalog):
            done, thread = self.hold_thread(fn=lambda outer_claimed, outer_release:
                (outer_claimed.set(), campaign.warm_task_catalog()))
            try:
                self.assertTrue(claimed.wait(10))
                with self.assertRaises(CampaignStateLeaseError):
                    with campaign_state_lease(exclusive=True):
                        self.fail("cold catalog can still publish its cache")
                self.assertFalse(cache.exists())
            finally:
                release.set()
                done.set()
                thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(cache.read_bytes(), b"inert derived catalog")
        with campaign_state_lease(exclusive=True), \
                patch.object(campaign, "_ranked_pools", side_effect=AssertionError("cold writer forbidden")):
            refused = []
            def blocked_warmup():
                try:
                    campaign.warm_task_catalog()
                except CampaignStateLeaseError as exc:
                    refused.append(exc.code)
            thread = threading.Thread(target=blocked_warmup)
            thread.start()
            thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(refused, ["campaign_reset_busy"])
        self.assertEqual(cache.read_bytes(), b"inert derived catalog")


    def test_pending_protocol_reads_actual_journal_under_shared_until_body_finishes(self):
        journal = upload.save_sidecar(self.row())
        before = journal.read_bytes()

        @pending_protocol
        def inert_resume(session, claimed, release, *, account_email=None,
                         required_org_key=None, session_ids=None):
            self.assertEqual(upload.load_sidecar("inert-session")["org_key"], "local-org")
            claimed.set()
            if not release.wait(10):
                raise AssertionError("fixture release timed out")

        release, thread = self.hold_thread(fn=lambda claimed, done:
            inert_resume(SimpleNamespace(email="local@example.invalid"), claimed, done,
                         required_org_key="local-org", session_ids={"inert-session"}))
        with self.assertRaises(CampaignStateLeaseError):
            with campaign_state_lease(exclusive=True):
                self.fail("resume owns a selected session")
        self.assertEqual(journal.read_bytes(), before)
        release.set()
        thread.join(10)
        with campaign_state_lease(exclusive=True):
            pass

    def test_busy_writer_hooks_do_not_create_state_or_inner_locks(self):
        release, thread = self.hold_thread(exclusive=True)
        log = CampaignLog("2026-10-06T00:00:00Z", ["local@example.invalid"])
        actions = [
            lambda: log.save(),
            lambda: upload.save_sidecar(self.row()),
            lambda: recording_timeline.reserve("local@example.invalid", 30, now=100),
            lambda: sent_registry.mark_sent("minute|local-task", "local-clip", "local@example.invalid"),
            lambda: campaign_start_store.claim("local-start", kind="dataset", receipt_id="local-receipt",
                body={}, bindings={"accounts": [{"email": "local@example.invalid", "org_key": "local-org"}],
                                   "tasks": ["local-task"]}),
            lambda: recovery.reconcile_confirmed(),
        ]
        for action in actions:
            with self.subTest(action=actions.index(action)):
                with self.assertRaises(CampaignStateLeaseError):
                    action()
        with self.assertRaises(original_capture.OriginalCaptureError):
            with original_capture._exclusive_operation():
                self.fail("reservation lock must never be acquired")
        self.assertEqual({p.name for p in self.data.iterdir()}, {".campaign-state.lock"})
        self.assertIsNone(log._path)
        release.set()
        thread.join(10)
        # The same public writer works after reset releases the barrier.
        path = log.save()
        self.assertTrue(path.exists())
        recording_timeline.reserve("local@example.invalid", 30, now=100)
        self.assertTrue(recording_timeline.timeline_path().exists())

    def test_pending_marker_blocks_all_roots_and_backups_but_allows_exclusive_retry(self):
        candidates = (self.data, self.media, self.data.parent / "backups", self.media.parent / "backups")
        for directory in candidates:
            with self.subTest(root=str(directory.relative_to(self.root))):
                directory.mkdir(parents=True, exist_ok=True)
                marker = directory / ".campaign-reset-pending"
                marker.mkdir()
                evidence = marker / "old-state.json"
                evidence.write_bytes(b"preserve incomplete cleanup")
                with self.assertRaises(CampaignStateLeaseError) as caught:
                    upload.save_sidecar(self.row())
                self.assertEqual(caught.exception.code, "campaign_reset_incomplete")
                self.assertFalse((self.data / "sidecars").exists())
                with campaign_state_lease(exclusive=True):
                    with campaign_state_lease():
                        self.assertEqual(evidence.read_bytes(), b"preserve incomplete cleanup")
                evidence.unlink()
                marker.rmdir()
                with campaign_state_lease():
                    pass

    def test_pending_marker_is_checked_after_os_barrier_is_acquired(self):
        # A pending gate must not make exclusive retries themselves unusable.
        marker = self.data / ".campaign-reset-pending"
        with campaign_state_lease(exclusive=True):
            marker.mkdir()
            self.assertEqual(upload.list_sidecars(), [])
        blocked = self.child()
        out, err = blocked.communicate(timeout=10)
        self.assertEqual(blocked.returncode, 4, err)
        self.assertEqual(out.strip(), "campaign_reset_incomplete")
        retry = self.child(exclusive=True)
        out, err = retry.communicate(timeout=10)
        self.assertEqual(retry.returncode, 0, err)
        marker.rmdir()
        with campaign_state_lease(exclusive=True):
            pass

    def test_recovery_reset_rejects_live_terminal_thread_and_clears_only_after_exit(self):
        runner = recovery.RecoveryRunner()
        release = threading.Event()
        thread = threading.Thread(target=lambda: release.wait(10))
        thread.start()
        self.addCleanup(lambda: (release.set(), thread.join(10)))
        runner._thread = thread
        runner._state = {"state": "done", "email": "local@example.invalid", "error": None,
                         "result": {"reconciled": 1}}
        before = runner.snapshot()
        with self.assertRaises(RuntimeError):
            runner.reset_idle()
        self.assertEqual(runner.snapshot(), before)
        release.set()
        thread.join(10)
        runner.reset_idle()
        self.assertEqual(runner.snapshot(), {"state": "idle", "email": None, "error": None})
        self.assertIsNone(runner._thread)


if __name__ == "__main__":
    unittest.main()
