"""Real temporary-process locks, crash release and pre-bootstrap refusal."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moneymin import config, web
from moneymin.state_lease import StateLeaseError, service_state_lease


class ServiceStateLeaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def child(self, root, wait=False):
        code = '''
import sys
from pathlib import Path
from moneymin.state_lease import StateLeaseError, service_state_lease
try:
    with service_state_lease(Path(sys.argv[1])):
        print("claimed", flush=True)
        if sys.argv[2] == "wait":
            sys.stdin.readline()
except StateLeaseError:
    print("blocked", flush=True)
    sys.exit(4)
'''
        process = subprocess.Popen([sys.executable, "-u", "-c", code, str(root), "wait" if wait else "exit"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, cwd=Path(__file__).resolve().parents[1])
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        return process

    def test_real_process_cannot_claim_same_root_and_can_claim_different_root(self):
        with service_state_lease(self.root):
            same = self.child(self.root)
            out, err = same.communicate(timeout=10)
            self.assertEqual(same.returncode, 4, err)
            self.assertEqual(out.strip(), "blocked")
            other = self.child(self.root / "other")
            out, err = other.communicate(timeout=10)
            self.assertEqual(other.returncode, 0, err)
            self.assertEqual(out.strip(), "claimed")
        successor = self.child(self.root)
        out, err = successor.communicate(timeout=10)
        self.assertEqual(successor.returncode, 0, err)
        self.assertEqual(out.strip(), "claimed")

    def test_crashed_holder_releases_os_lock_without_deleting_marker(self):
        holder = self.child(self.root, wait=True)
        self.assertEqual(holder.stdout.readline().strip(), "claimed")
        with self.assertRaises(StateLeaseError):
            with service_state_lease(self.root):
                pass
        holder.kill()
        holder.communicate(timeout=10)
        marker = self.root / "service-state.lock"
        self.assertTrue(marker.exists())
        before = marker.read_bytes()
        with service_state_lease(self.root):
            self.assertTrue(marker.exists())
        self.assertEqual(marker.read_bytes(), before)
        self.assertTrue(marker.exists())

    def test_failure_happens_before_bootstrap_threads_or_serving(self):
        with service_state_lease(self.root), patch.object(config, "DATA_DIR", self.root), \
             patch.object(web, "_require_local_api_token"), \
             patch.object(web, "_run_claimed_service") as bootstrap:
            with self.assertRaises(StateLeaseError):
                web.run_webui(port=8877)
        bootstrap.assert_not_called()

    def test_exception_inside_service_releases_lease(self):
        with self.assertRaises(RuntimeError):
            with service_state_lease(self.root):
                raise RuntimeError("fixture")
        with service_state_lease(self.root):
            pass


if __name__ == "__main__":
    unittest.main()
