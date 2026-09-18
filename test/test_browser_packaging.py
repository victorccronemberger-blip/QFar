import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == "nt", "Windows release packaging")
class BrowserPackagingTests(unittest.TestCase):
    def test_exact_revisions_and_missing_component(self):
        helper = Path(__file__).resolve().parents[1] / "scripts" / "browser_parts.ps1"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            names = ["chromium", "chromium-headless-shell", "ffmpeg", "winldd"]
            manifest = root / "browsers.json"
            manifest.write_text(json.dumps({"browsers": [
                {"name": name, "revision": "1234"} for name in names
            ]}), encoding="utf-8")
            for name in names:
                for revision in ("999", "1234", "9999"):
                    (root / (name.replace("-", "_") + "-" + revision)).mkdir()
            env = dict(os.environ, QMONEY_TEST_HELPER=str(helper),
                       QMONEY_TEST_MANIFEST=str(manifest), QMONEY_TEST_SOURCE=str(root))
            command = '''
$ErrorActionPreference = 'Stop'
. $env:QMONEY_TEST_HELPER
Get-QMoneyBrowserParts -ManifestPath $env:QMONEY_TEST_MANIFEST -BrowserSource $env:QMONEY_TEST_SOURCE | ForEach-Object { $_.Name }
'''
            def run():
                return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive",
                                       "-Command", command], env=env, capture_output=True,
                                      text=True, timeout=30)

            result = run()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(),
                             [name.replace("-", "_") + "-1234" for name in names])
            (root / "chromium_headless_shell-1234").rmdir()
            self.assertNotEqual(run().returncode, 0)
