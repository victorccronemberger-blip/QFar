import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from moneymin import crowtado


class SignupProcessTests(unittest.TestCase):
    def test_real_process_is_reaped_before_signup_returns(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.run_failed_signup(process)
            self.assertIsNotNone(process.returncode)
            self.assertIsNotNone(process.poll())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    def run_failed_signup(self, process):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(crowtado, "CHROME_PROFILE", Path(tmp)), \
             patch.object(crowtado, "_chrome_exe", return_value="test-chrome"), \
             patch.object(crowtado, "_wait_port", side_effect=crowtado.CrowtadoError("startup failed")), \
             patch("subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(crowtado.CrowtadoError, "startup failed"):
                crowtado.criar_conta("test@example.invalid", "test-only")

    def test_failure_waits_for_browser_exit(self):
        process = Mock()
        process.poll.return_value = None
        self.run_failed_signup(process)
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)
        process.kill.assert_not_called()

    def test_unresponsive_browser_is_killed_and_reaped(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("test-chrome", 5), 0]
        self.run_failed_signup(process)
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)

    def test_browser_already_exited_is_not_terminated_again(self):
        process = Mock()
        process.poll.return_value = 1
        self.run_failed_signup(process)
        process.terminate.assert_not_called()
        process.wait.assert_called_once_with(timeout=5)
