import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts.verify_packaged_service import _ProbeProcess, probe


@unittest.skipUnless(os.name == "nt", "Windows package process-tree ownership")
class PackagedServiceHarnessTests(unittest.TestCase):
    CHILD = r'''
import ctypes, os, sys, time
from ctypes import wintypes
from pathlib import Path
root = Path(sys.argv[1])
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
kernel.CreateFileW.restype = wintypes.HANDLE
handle = kernel.CreateFileW(str(root / "service-state.lock"), 0x40000000, 0, None, 2, 0x80, None)
if handle == ctypes.c_void_p(-1).value:
    raise ctypes.WinError(ctypes.get_last_error())
(root / "child-ready").write_text(str(os.getpid()), encoding="utf-8")
time.sleep(60)
'''

    def wait_for(self, condition):
        deadline = time.monotonic() + 5
        while not condition():
            if time.monotonic() >= deadline:
                self.fail("Inert process fixture did not become ready")
            time.sleep(.02)

    def assert_tree_releases_lock(self, parent_exits):
        import _winapi
        with tempfile.TemporaryDirectory(prefix="qmoney-harness-fixture-") as directory:
            root = Path(directory) / "cliente com espaços órfão"
            root.mkdir()
            # This process is ours too, but deliberately outside the probe job.
            # Cleanup must not affect it, even though it has the same image name.
            outsider = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                creationflags=subprocess.CREATE_NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            process = None
            child_handle = None
            try:
                parent = ("import subprocess,sys,time; "
                    "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); "
                    + ("sys.exit(0)" if parent_exits else "time.sleep(60)"))
                process = _ProbeProcess([sys.executable, "-c", parent, self.CHILD, str(root)],
                                        cwd=root, env=dict(os.environ))
                self.wait_for(lambda: (root / "child-ready").exists())
                child_pid = int((root / "child-ready").read_text(encoding="utf-8"))
                child_handle = _winapi.OpenProcess(0x100000, False, child_pid)  # SYNCHRONIZE
                lock = root / "service-state.lock"
                with self.assertRaises(PermissionError) as locked:
                    lock.unlink()
                self.assertEqual(locked.exception.winerror, 32)
                if parent_exits:
                    self.wait_for(lambda: process.poll() is not None)
                    self.assertEqual(process.returncode, 0)
                else:
                    self.assertIsNone(process.poll())
                process.close()
                self.assertEqual(_winapi.WaitForSingleObject(child_handle, 0), _winapi.WAIT_OBJECT_0)
                lock.unlink()  # No retries/ignore_errors: the tree must already be gone.
                self.assertIsNone(outsider.poll())
                process.close()  # Idempotent teardown must not target a reused PID.
            finally:
                if process is not None:
                    process.close()
                if child_handle is not None:
                    _winapi.CloseHandle(child_handle)
                outsider.terminate()
                outsider.wait(timeout=5)

    def test_bootloader_and_child_exit_before_directory_cleanup(self):
        self.assert_tree_releases_lock(parent_exits=False)

    def test_exited_bootloader_does_not_skip_surviving_child(self):
        self.assert_tree_releases_lock(parent_exits=True)

    def test_launch_failure_closes_its_empty_job(self):
        with tempfile.TemporaryDirectory(prefix="qmoney-harness-missing-") as directory:
            root = Path(directory)
            with self.assertRaises(FileNotFoundError):
                _ProbeProcess([str(root / "missing-service.exe")], cwd=root, env=dict(os.environ))

    def test_failed_handshake_still_closes_owned_tree(self):
        with tempfile.TemporaryDirectory(prefix="qmoney-harness-handshake-") as directory:
            root = Path(directory)
            with patch("scripts.verify_packaged_service._ProbeProcess") as process_type:
                process = process_type.return_value
                process.poll.return_value = 17
                process.returncode = 17
                with self.assertRaisesRegex(RuntimeError, "exited before handshake"):
                    probe(root / "inert-service.exe", root / "customer", root / "library", [])
                process.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
