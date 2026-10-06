"""Isolated smoke of a built Windows service in fresh customer directories."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid


def probe(service: Path, user_root: Path, library: Path, expected: list[str]) -> None:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    token = uuid.uuid4().hex
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("QMONEY_", "MINUTE_", "AWS_", "HOSTINGER_", "EGO4D_", "CROWTADO_"))}
    environment.update(QMONEY_USER_ROOT=str(user_root), QMONEY_LIBRARY_ROOT=str(library),
                       QMONEY_RUNTIME_ROOT=str(service.parent), QMONEY_LOCAL_API_TOKEN=token,
                       QMONEY_APP_VERSION=os.environ.get("QMONEY_VERSION", "2.0.27").lstrip("v"), MINUTE_VPN_ENFORCE="0",
                       MINUTE_REQUIRE_CURL="0", MINUTE_PUBLISH_APP_OPENED="0",
                       AWS_SHARED_CREDENTIALS_FILE=str(user_root / "secrets/aws/credentials"),
                       AWS_CONFIG_FILE=str(user_root / "secrets/aws/config"), AWS_EC2_METADATA_DISABLED="true")
    user_root.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen([str(service), "--no-browser", "--host", "127.0.0.1", "--porta", str(port),
                                "--parent-pid", str(os.getpid())], cwd=user_root, env=environment,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(route, authenticated=True, method='GET'):
        request = urllib.request.Request(f"http://127.0.0.1:{port}{route}",
                    headers={"X-QMoney-Session": token} if authenticated else {}, method=method)
        with opener.open(request, timeout=3) as response:
            return json.load(response)

    try:
        deadline = time.monotonic() + 45
        while True:
            if process.poll() is not None:
                raise RuntimeError(f"Packaged service exited before handshake: {process.returncode}")
            try:
                assert get("/api/health")["ok"] is True
                break
            except (urllib.error.URLError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Packaged service did not start within 45 seconds") from None
                time.sleep(0.2)
        try:
            get("/api/accounts", authenticated=False)
            raise AssertionError("Unauthenticated account request was accepted")
        except urllib.error.HTTPError as error:
            assert error.code == 401
        local_files = get('/api/storage/library/items')
        assert local_files['schema'] == 1 and local_files['inventory_scope'] == 'local_media_files'
        assert local_files['file_count'] > 0
        assert all('path' in item and 'size_bytes' in item for item in local_files['items'])
        accounts = get("/api/accounts")["accounts"]
        assert sorted(account["email"] for account in accounts) == sorted(expected)
        assert "fixture-private-token" not in json.dumps(accounts)
        assert get("/api/campaigns/current")["state"] == "idle"
        assert get("/api/recovery")["items"] == []
        try:
            get('/api/library/ego4d', authenticated=False)
            raise AssertionError('Unauthenticated library request was accepted')
        except urllib.error.HTTPError as error:
            assert error.code == 401
        # These two recordings exist only in the temporary fixture directory.
        # Exercise SQLite/FTS in the frozen executable, not the source interpreter.
        index_deadline = time.monotonic() + 15
        while True:
            state = get('/api/library/ego4d/index', method='POST')
            if not state.get('loading'):
                assert state['state'] == 'ready' and state['videos'] == 2
                break
            if time.monotonic() >= index_deadline:
                raise RuntimeError('Packaged original library index did not finish')
            time.sleep(.1)
        results = get('/api/library/ego4d/videos?q=soup&min_s=300')
        assert results['provenance'] == 'ego4d_original' and results['total'] == 1
        assert results['items'][0]['duration_s'] == 600.123
        assert results['items'][0]['has_imu'] is None
        assert results['items'][0]['imu_local'] is False
        prepared = get('/api/library/ego4d/prepared')
        assert prepared['schema'] == 1 and prepared['inventory_scope'] == 'local_prepared_media'
        assert prepared['physical_clip_count'] == 0 and prepared['items'] == []
        assert prepared['counts']['ready'] == 0
        try:
            get('/api/library/ego4d/prepared', authenticated=False)
            raise AssertionError('Unauthenticated prepared inventory request was accepted')
        except urllib.error.HTTPError as error:
            assert error.code == 401
    finally:
        # Target only the process tree created above, including the onefile child.
        if process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
            process.wait(timeout=15)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("service", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    service = args.service.resolve(strict=True)
    if os.name != "nt":
        raise RuntimeError("This package verification requires Windows")
    with tempfile.TemporaryDirectory(prefix="qmoney-package-test-") as directory:
        root = Path(directory)
        library = root / "library"
        (library / "secrets").mkdir(parents=True)
        (library / "secrets/token_foreign.json").write_text(json.dumps({"email": "foreign@example.invalid"}), encoding="utf-8")
        (library / ".env").write_text("HOSTINGER_MAIL_TOKEN=foreign-library-secret", encoding="utf-8")
        ego = library / 'data/ego4d'
        ego.mkdir(parents=True)
        (ego / 'ego4d.json').write_text(json.dumps({'videos': [
            {'video_uid': 'fixture-original-1', 'duration_sec': 600.123, 'scenarios': ['Cooking']},
            {'video_uid': 'fixture-original-2', 'duration_sec': 90, 'has_imu': False},
        ]}), encoding='utf-8')
        (ego / 'clips.csv').write_text('exported_clip_uid,parent_video_uid,parent_start_sec,parent_end_sec\n'
                                      'fixture-clip,fixture-original-1,10,310\n', encoding='utf-8')
        (ego / 'timed_narrations.jsonl').write_text(json.dumps({
            'video_uid': 'fixture-original-1', 'events': [[20, '#C stirs soup']]}), encoding='utf-8')
        customer = root / "customer-a"
        probe(service, customer, library, [])
        credentials = customer / "secrets/token_fixture.json"
        original = json.dumps({"email": "fixture@example.invalid", "idToken": "fixture-private-token"}).encode()
        credentials.write_bytes(original)
        probe(service, customer, library, ["fixture@example.invalid"])
        assert credentials.read_bytes() == original
        probe(service, root / "customer-b", library, [])
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({"passed": True, "checks": ["fresh_installation", "restart_preserves_credentials",
        "separate_customer_roots", "local_api_authentication", "empty_recovery", "no_campaign_started",
        "original_library_index_and_fts", "original_duration_and_unknown_sensor_state",
        "prepared_library_inventory", "prepared_library_authentication",
        "general_local_media_inventory"]}, indent=2), encoding="utf-8")
    print("Packaged service checks passed; no uploads or withdrawals requested.")


if __name__ == "__main__":
    main()
