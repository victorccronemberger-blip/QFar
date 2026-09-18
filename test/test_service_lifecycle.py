import ctypes
import io
import os
import subprocess
import sys
import unittest
from ctypes import wintypes
from types import SimpleNamespace
from unittest.mock import Mock, patch

from moneymin import web


class ServiceLifecycleTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows process handles")
    def test_real_parent_exit_stops_watching_process(self):
        code = '''
import subprocess, sys, time
from moneymin.web import _watch_parent
parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1)"])
_watch_parent(parent.pid)
time.sleep(5)
sys.exit(7)
'''
        result = subprocess.run([sys.executable, "-c", code], timeout=15,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_closed_console_does_not_interrupt_work(self):
        stream = io.StringIO()
        stream.close()
        safe = web._SafeStream(stream)
        self.assertEqual(safe.write("progresso"), 9)
        safe.flush()

    def run_watcher(self, handle, wait_result=0, last_error=0):
        kernel = Mock()
        kernel.OpenProcess.return_value = handle
        kernel.WaitForSingleObject.return_value = wait_result
        exit_process = Mock()
        with patch.object(web, "os", SimpleNamespace(name="nt", _exit=exit_process)), \
             patch.object(ctypes, "WinDLL", return_value=kernel, create=True), \
             patch.object(ctypes, "get_last_error", return_value=last_error, create=True), \
             patch.object(web.threading, "Thread") as thread:
            web._watch_parent(123)
            thread.call_args.kwargs["target"]()
        return kernel, exit_process

    def test_parent_exit_closes_handle_and_stops_service(self):
        handle = 0x123456789
        kernel, exit_process = self.run_watcher(handle)
        self.assertIs(kernel.OpenProcess.restype, wintypes.HANDLE)
        self.assertEqual(kernel.WaitForSingleObject.argtypes[0], wintypes.HANDLE)
        kernel.WaitForSingleObject.assert_called_once_with(handle, 0xFFFFFFFF)
        kernel.CloseHandle.assert_called_once_with(handle)
        exit_process.assert_called_once_with(0)

    def test_failed_wait_does_not_kill_service(self):
        kernel, exit_process = self.run_watcher(42, wait_result=0xFFFFFFFF)
        kernel.CloseHandle.assert_called_once_with(42)
        exit_process.assert_not_called()

    def test_only_missing_parent_causes_exit_when_open_fails(self):
        for error in (5, 87):
            with self.subTest(error=error):
                kernel, exit_process = self.run_watcher(None, last_error=error)
                kernel.WaitForSingleObject.assert_not_called()
                kernel.CloseHandle.assert_not_called()
                self.assertEqual(exit_process.called, error == 87)
