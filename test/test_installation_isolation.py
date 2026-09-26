"""Fresh processes exercise installation roots, not patched module globals."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


CHILD = r'''
import json, os, socket
from pathlib import Path
from unittest.mock import patch
with patch.object(socket.socket, "connect", side_effect=AssertionError("network prohibited")):
    from moneymin import config, upload
    from moneymin.web import server
    from moneymin.atomic_io import save_json
    token = os.environ["QMONEY_LOCAL_API_TOKEN"]
    client = server.create_app().test_client()
    headers = {"X-QMoney-Session": token}
    accounts = client.get("/api/accounts", headers=headers)
    assert accounts.status_code == 200, accounts.get_json()
    before = [row["email"] for row in accounts.get_json()["accounts"]]
    email = os.environ.get("FIXTURE_ADD_ACCOUNT")
    if email:
        save_json(config.token_path(email), {"email": email, "idToken": "fixture-private-token"})
    after = client.get("/api/accounts", headers=headers)
    assert "fixture-private-token" not in after.get_data(as_text=True)
    assert client.get("/api/accounts").status_code == 401
    assert client.get("/api/accounts", headers={"X-QMoney-Session":"different-installation"}).status_code == 401
    state = client.get("/api/campaigns/current", headers=headers)
    assert state.status_code == 200 and state.get_json()["state"] == "idle"
    recovery = client.get("/api/recovery", headers=headers)
    assert recovery.status_code == 200, recovery.get_json()
    assert recovery.get_json()["items"] == []
    assert upload.sidecars_dir().resolve() == (config.DATA_DIR / "sidecars").resolve()
    assert config.DATA_DIR.resolve() == (Path(os.environ["QMONEY_USER_ROOT"]) / "data").resolve()
    assert config.MEDIA_DATA_DIR.resolve() == (Path(os.environ["QMONEY_LIBRARY_ROOT"]) / "data").resolve()
    assert os.environ.get("INSTALLATION_SENTINEL") != "foreign-library-secret"
    print(json.dumps({"before":before, "after":[row["email"] for row in after.get_json()["accounts"]]}))
'''


class InstallationIsolationTests(unittest.TestCase):
    def launch(self, user_root, library, email=None):
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("QMONEY_", "MINUTE_", "AWS_", "HOSTINGER_", "EGO4D_", "CROWTADO_", "FIXTURE_"))}
        environment.update(QMONEY_USER_ROOT=str(user_root), QMONEY_LIBRARY_ROOT=str(library),
                           QMONEY_LOCAL_API_TOKEN="fixture-installation-token", MINUTE_VPN_ENFORCE="0",
                           MINUTE_REQUIRE_CURL="0", MINUTE_PUBLISH_APP_OPENED="0")
        if email:
            environment["FIXTURE_ADD_ACCOUNT"] = email
        result = subprocess.run([sys.executable, "-c", CHILD], env=environment,
                                cwd=Path(__file__).resolve().parents[1], timeout=30,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip())

    def test_clean_install_restart_and_second_customer_share_only_media(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "shared-library"
            (library / "secrets").mkdir(parents=True)
            (library / ".env").write_text("INSTALLATION_SENTINEL=foreign-library-secret", encoding="utf-8")
            (library / "secrets" / "token_foreign.json").write_text(json.dumps({
                "email": "foreign@example.invalid", "idToken": "foreign-token"}), encoding="utf-8")
            first = self.launch(root / "customer-a", library, "a@example.invalid")
            self.assertEqual(first, {"before": [], "after": ["a@example.invalid"]})
            second = self.launch(root / "customer-b", library, "b@example.invalid")
            self.assertEqual(second, {"before": [], "after": ["b@example.invalid"]})
            restarted = self.launch(root / "customer-a", library)
            self.assertEqual(restarted, {"before": ["a@example.invalid"], "after": ["a@example.invalid"]})
            self.assertTrue((library / "secrets" / "token_foreign.json").exists())
