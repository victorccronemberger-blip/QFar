"""Utility imports must not load personal configuration or the app pipeline."""
import subprocess
import sys
import unittest
from pathlib import Path


class PackageImportIsolationTests(unittest.TestCase):
    def run_isolated(self, body):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, "-c", body], cwd=root,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_import_package_and_atomic_io_do_not_import_configuration(self):
        self.run_isolated('''
import importlib.abc, sys
class DenyConfig(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname in {"moneymin.config", "moneymin.campaign", "moneymin.minute_api"}:
            raise AssertionError("utility import entered app bootstrap")
sys.meta_path.insert(0, DenyConfig())
import moneymin
from moneymin.atomic_io import load_json_state
assert "moneymin.config" not in sys.modules
assert "moneymin.upload" not in sys.modules
assert set(moneymin.__all__) <= set(dir(moneymin))
try:
    moneymin.unknown_public_module
except AttributeError:
    pass
else:
    raise AssertionError("unknown export accepted")
''')

    def test_existing_public_module_access_remains_compatible(self):
        self.run_isolated('''
import moneymin
assert "config" not in moneymin.__dict__
first = moneymin.config
from moneymin import config
import moneymin.config as direct
assert first is config is direct
assert moneymin.config is first
from moneymin import upload
assert upload is moneymin.upload
''')


if __name__ == "__main__":
    unittest.main()
