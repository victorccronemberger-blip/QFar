"""Verificações de ferramentas não devem abrir consoles nem ficar penduradas."""
import os
import subprocess
import sys
import unittest
from unittest import mock

from moneymin import readiness
from moneymin.web import server


class BackgroundProcessTests(unittest.TestCase):
    def test_health_does_not_launch_tools_or_read_user_data(self):
        client = server.create_app().test_client()
        with mock.patch.dict(os.environ, {"QMONEY_APP_VERSION": "1.0.26"}), \
             mock.patch.object(server.readiness, "campaign_readiness") as ready, \
             mock.patch.object(server.readiness, "_binary_works") as binary, \
             mock.patch.object(server, "_list_accounts") as accounts, \
             mock.patch.object(server, "_storage_snapshot") as storage:
            for _ in range(25):
                result = client.get("/api/health")
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.get_json(), {
                    "ok": True, "service": {"app_version": "1.0.26"}})
            for operation in (ready, binary, accounts, storage):
                operation.assert_not_called()

    def test_readiness_hides_console_and_limits_lifetime(self):
        with mock.patch.object(readiness.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertTrue(readiness._binary_works("ffprobe.exe"))
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["ffprobe.exe", "-version"])
        self.assertEqual(kwargs["creationflags"],
                         getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(kwargs["timeout"], 15)
        for stream in ("stdin", "stdout", "stderr"):
            self.assertEqual(kwargs[stream], subprocess.DEVNULL)

    def test_tool_failures_do_not_escape_readiness(self):
        for error in (FileNotFoundError(), OSError(),
                      subprocess.TimeoutExpired("ffprobe.exe", 15)):
            with self.subTest(error=type(error).__name__), \
                 mock.patch.object(readiness.subprocess, "run", side_effect=error):
                self.assertFalse(readiness._binary_works("ffprobe.exe"))

    def test_nonzero_exit_is_not_ready(self):
        with mock.patch.object(readiness.subprocess, "run") as run:
            run.return_value.returncode = 1
            self.assertFalse(readiness._binary_works("ffprobe.exe"))

    def test_timed_out_child_is_reaped(self):
        actual_run, actual_popen = subprocess.run, subprocess.Popen
        children = []
        def track_child(*args, **kwargs):
            child = actual_popen(*args, **kwargs)
            children.append(child)
            return child
        def stalled_tool(command, **kwargs):
            kwargs["timeout"] = 0.15
            return actual_run([sys.executable, "-c", "import time; time.sleep(60)"],
                              **kwargs)
        try:
            with mock.patch.object(readiness.subprocess, "Popen", side_effect=track_child), \
                 mock.patch.object(readiness.subprocess, "run", side_effect=stalled_tool):
                self.assertFalse(readiness._binary_works("ffprobe.exe"))
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)

    @unittest.skipUnless(os.name == "nt", "Windows console regression")
    def test_actual_readiness_child_has_no_console(self):
        # Execute the exact launch options from readiness with a harmless child.
        actual_run = subprocess.run
        def probe_console(command, **kwargs):
            return actual_run([
                sys.executable, "-c",
                "import ctypes,sys; "
                "sys.exit(1 if ctypes.windll.kernel32.GetConsoleWindow() else 0)",
            ], **kwargs)
        with mock.patch.object(readiness.subprocess, "run", side_effect=probe_console):
            self.assertTrue(readiness._binary_works("ffprobe.exe"))


if __name__ == "__main__":
    unittest.main()
