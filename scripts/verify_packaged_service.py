"""Isolated smoke of a built Windows service in fresh customer directories."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile


class _ProbeProcess:
    """Own only this probe's Windows job, including a onefile bootloader child."""

    def __init__(self, command: list[str], *, cwd: Path, env: dict[str, str]):
        if os.name != "nt":
            raise RuntimeError("This package verification requires Windows")
        import ctypes
        from ctypes import wintypes
        import _winapi
        import msvcrt

        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                        ("max_working_set", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class Limits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io_counters", ctypes.c_uint64 * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [("user_time", ctypes.c_int64), ("kernel_time", ctypes.c_int64),
                        ("period_user_time", ctypes.c_int64), ("period_kernel_time", ctypes.c_int64),
                        ("page_faults", wintypes.DWORD), ("total_processes", wintypes.DWORD),
                        ("active_processes", wintypes.DWORD), ("terminated_processes", wintypes.DWORD)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, arguments, result in (
            ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            ("SetInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            ("QueryInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            ("IsProcessInJob", [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL),
            ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            ("ResumeThread", [wintypes.HANDLE], wintypes.DWORD),
        ):
            function = getattr(kernel, name)
            function.argtypes, function.restype = arguments, result
        self._kernel, self._ctypes, self._winapi = kernel, ctypes, _winapi
        self._accounting = Accounting
        self._bool = wintypes.BOOL
        self._handle = self._job = None
        self.returncode = None
        thread = None
        try:
            self._job = kernel.CreateJobObjectW(None, None)
            if not self._job:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = Limits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
            if not kernel.SetInformationJobObject(self._job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            # Keep the primary thread handle, which Popen normally closes. Attach
            # the suspended process before any onefile child can be created.
            with open(os.devnull, "r+b") as null:
                null_handle = msvcrt.get_osfhandle(null.fileno())
                os.set_handle_inheritable(null_handle, True)
                startup = subprocess.STARTUPINFO()
                startup.dwFlags = subprocess.STARTF_USESTDHANDLES
                startup.hStdInput = startup.hStdOutput = startup.hStdError = null_handle
                startup.lpAttributeList = {"handle_list": [null_handle]}
                self._handle, thread, self.pid, _ = _winapi.CreateProcess(
                    None, subprocess.list2cmdline(command), None, None, True,
                    subprocess.CREATE_NO_WINDOW | 0x4, env, str(cwd), startup)  # CREATE_SUSPENDED
            if not kernel.AssignProcessToJobObject(self._job, self._handle):
                error = ctypes.WinError(ctypes.get_last_error())
                _winapi.TerminateProcess(self._handle, 1)
                _winapi.WaitForSingleObject(self._handle, 15000)
                raise error
            if kernel.ResumeThread(thread) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise
        finally:
            if thread is not None:
                _winapi.CloseHandle(thread)

    def poll(self):
        if self.returncode is None and self._winapi.WaitForSingleObject(self._handle, 0) == self._winapi.WAIT_OBJECT_0:
            self.returncode = self._winapi.GetExitCodeProcess(self._handle)
        return self.returncode

    def _owned_process_handles(self) -> list[int]:
        ctypes = self._ctypes
        capacity = 8
        while True:
            class ProcessIds(ctypes.Structure):
                _fields_ = [("assigned", ctypes.c_uint32), ("count", ctypes.c_uint32),
                            ("pids", ctypes.c_size_t * capacity)]

            members = ProcessIds()
            success = self._kernel.QueryInformationJobObject(
                self._job, 3, ctypes.byref(members), ctypes.sizeof(members), None)
            if success:
                break
            if ctypes.get_last_error() != 234:  # ERROR_MORE_DATA: a descendant was added.
                raise ctypes.WinError(ctypes.get_last_error())
            capacity = max(capacity * 2, members.assigned)
        handles = []
        try:
            for pid in members.pids[:members.count]:
                try:
                    # Membership is checked against the retained process handle,
                    # so a recycled PID cannot make us wait on another program.
                    handle = self._winapi.OpenProcess(0x101000, False, pid)
                except OSError as error:
                    if error.winerror == 87:  # Process exited between snapshot and open.
                        continue
                    raise
                belongs = self._bool()
                checked = self._kernel.IsProcessInJob(handle, self._job, ctypes.byref(belongs))
                if checked and belongs.value:
                    handles.append(handle)
                else:
                    self._winapi.CloseHandle(handle)
                    if not checked:
                        raise ctypes.WinError(ctypes.get_last_error())
            return handles
        except BaseException:
            for handle in handles:
                self._winapi.CloseHandle(handle)
            raise

    def close(self, timeout: float = 15) -> None:
        if self._job is None:
            return
        handles = []
        try:
            # This also terminates descendants after the bootloader already
            # exited. PID/name enumeration cannot provide that ownership proof.
            handles = self._owned_process_handles()
            if not self._kernel.TerminateJobObject(self._job, 1):
                raise self._ctypes.WinError(self._ctypes.get_last_error())
            deadline = time.monotonic() + timeout
            while True:
                accounting = self._accounting()
                if not self._kernel.QueryInformationJobObject(
                        self._job, 1, self._ctypes.byref(accounting), self._ctypes.sizeof(accounting), None):
                    raise self._ctypes.WinError(self._ctypes.get_last_error())
                if accounting.active_processes == 0:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("Probe process tree did not exit before temporary-directory cleanup")
                time.sleep(.02)
            # Job accounting can reach zero while a process is still completing
            # termination and releasing its file handles. Wait for real exits.
            for handle in handles:
                remaining = max(0, int((deadline - time.monotonic()) * 1000))
                if self._winapi.WaitForSingleObject(handle, remaining) != self._winapi.WAIT_OBJECT_0:
                    raise RuntimeError("Probe child did not release its handles before temporary-directory cleanup")
        finally:
            for handle in handles:
                self._winapi.CloseHandle(handle)
            if self._handle is not None:
                self._winapi.CloseHandle(self._handle)
                self._handle = None
            self._winapi.CloseHandle(self._job)
            self._job = None


def probe(service: Path, user_root: Path, library: Path, expected: list[str], *, test_manifest=False, portable_setup=False) -> None:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    token = uuid.uuid4().hex
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("QMONEY_", "MINUTE_", "AWS_", "HOSTINGER_", "EGO4D_", "CROWTADO_", "NYMERIA_"))
                   and key not in {'PYTHONPATH', 'PYTHONHOME', 'PYTHONUSERBASE'}}
    environment['PYTHONNOUSERSITE'] = '1'
    environment.update(QMONEY_USER_ROOT=str(user_root), QMONEY_LIBRARY_ROOT=str(library),
                       QMONEY_RUNTIME_ROOT=str(service.parent), QMONEY_LOCAL_API_TOKEN=token,
                       QMONEY_APP_VERSION=os.environ.get("QMONEY_VERSION", "2.0.27").lstrip("v"), MINUTE_VPN_ENFORCE="0",
                       MINUTE_REQUIRE_CURL="0", MINUTE_PUBLISH_APP_OPENED="0",
                       AWS_SHARED_CREDENTIALS_FILE=str(user_root / "secrets/aws/credentials"),
                       AWS_CONFIG_FILE=str(user_root / "secrets/aws/config"), AWS_EC2_METADATA_DISABLED="true")
    user_root.mkdir(parents=True, exist_ok=True)
    if portable_setup:
        # Companion supplied privately beside a relocated app. Only the small
        # verified source catalog is seeded; acquiring VRS would hit an inert
        # fixture URL and fail this check. No external Python/SDK is available.
        portable_app = user_root / "relocated app"
        portable_app.mkdir()
        groups = {}
        annotation_zip = io.BytesIO()
        with zipfile.ZipFile(annotation_zip, "w") as bundle:
            bundle.writestr("narration/atomic_action.csv", "start_time,end_time,Describe my atomic actions\n")
        for name, data in {
                "metadata_json": b'{"uid":"native_setup","head_duration_sec":600}',
                "narration": annotation_zip.getvalue(),
                "timesync_and_imu": b"unacquired-real-IMU-required",
                "recording_head_data_data_vrs": b"unacquired-real-VRS-required"}.items():
            groups[name] = {"filename": name + ".zip", "sha1sum": hashlib.sha1(data).hexdigest(),
                            "file_size_bytes": len(data), "download_url": "https://fixture.fbcdn.net/" + name + ".zip"}
            if name in {"metadata_json", "narration"}:
                target = (library / "data/nymeria/native_setup/metadata.json" if name == "metadata_json"
                          else library / "data/nymeria/_catalog/archives/native_setup/narration.zip")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        (portable_app / "nymeria_plus_download_urls.json").write_text(
            json.dumps({"sequences": {"native_setup": groups}}), encoding="utf8")
        environment["QMONEY_PORTABLE_ROOT"] = str(portable_app)
    process = _ProbeProcess([str(service), "--no-browser", "--host", "127.0.0.1", "--porta", str(port),
                             "--parent-pid", str(os.getpid())], cwd=user_root, env=environment)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(route, authenticated=True, method='GET', body=None):
        request = urllib.request.Request(f"http://127.0.0.1:{port}{route}",
                    headers={**({"X-QMoney-Session": token} if authenticated else {}),
                             'Content-Type': 'application/json'}, method=method,
                    data=json.dumps(body).encode() if body is not None else None)
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
        nymeria = get('/api/readiness?dataset=nymeria')
        checks = {row['name']: row for row in nymeria['checks']}
        assert checks['SDK Nymeria']['status'] == 'ok'
        if not portable_setup:
            assert checks['Biblioteca Nymeria']['status'] == 'error'
        assert nymeria['ready'] is False
        source_catalog = get('/api/library/nymeria/sequences')
        assert source_catalog['total'] == (1 if portable_setup else 0)
        assert len(source_catalog['task_names']) == 49
        assert 'Furniture Assembly' in source_catalog['task_names']
        assert 'Gardening' not in source_catalog['task_names']
        assert source_catalog['task_catalog_source'] == 'local_snapshot_requires_campaign_preflight'
        if portable_setup:
            setup_deadline = time.monotonic() + 15
            while source_catalog['worker']['running']:
                assert get('/api/health')['ok'] is True
                if time.monotonic() >= setup_deadline:
                    raise RuntimeError('Native portable catalog setup did not finish')
                time.sleep(.1)
                source_catalog = get('/api/library/nymeria/sequences')
            assert source_catalog['setup']['state'] == 'imported'
            assert source_catalog['worker']['state'] == 'done'
            assert source_catalog['worker']['result']['sync']['errors'] == {}
            assert source_catalog['summary']['by_state']['downloaded'] == 0
            ready_catalog = get('/api/readiness?dataset=nymeria')
            assert next(row for row in ready_catalog['checks']
                        if row['name'] == 'Biblioteca Nymeria')['status'] == 'ok'
            assert not (library / 'data/nymeria/native_setup/recording_head/data/data.vrs').exists()
            assert not (library / 'data/nymeria/native_setup/recording_head/data/motion.vrs').exists()
        else:
            assert source_catalog['worker']['running'] is False
        try:
            get('/api/library/nymeria/sequences', authenticated=False)
            raise AssertionError('Unauthenticated Nymeria source inventory was accepted')
        except urllib.error.HTTPError as error:
            assert error.code == 401
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
        # Exercise the shipped reset, including its legacy import source. These
        # deliberately damaged receipts exist only in this temporary fixture.
        reset_files = []
        for directory in (user_root / 'data', library / 'data'):
            for name in ('campaign_package_fixture.json', 'sent_videos.json', 'sent_reset_history.json',
                         'start_requests.json', 'recording_timeline.json',
                         'original_capture_reservations.json', 'sidecars/fixture-session.json',
                         'sidecars/fixture-session.data.zip'):
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'{damaged-reset-fixture')
                reset_files.append(path)
        protected = [path for directory in (user_root / 'secrets', library / 'secrets', library / 'data/ego4d')
                     for path in directory.rglob('*') if path.is_file()]
        before = {path: path.read_bytes() for path in protected}
        try:
            get('/api/campaign/reset', authenticated=False, method='POST')
            raise AssertionError('Unauthenticated full reset was accepted')
        except urllib.error.HTTPError as error:
            assert error.code == 401
        reset_deadline = time.monotonic() + 45
        while True:
            try:
                cleared = get('/api/campaign/reset', method='POST')
                break
            except urllib.error.HTTPError as error:
                response = json.load(error)
                if error.code != 409 or response.get('error_code') != 'campaign_reset_busy' or time.monotonic() >= reset_deadline:
                    raise AssertionError('Packaged full reset did not finish') from None
                time.sleep(.2)
        assert cleared['ok'] is True and cleared['state'] == 'idle'
        assert all(not path.exists() for path in reset_files)
        assert {path: path.read_bytes() for path in protected} == before
        assert get('/api/logs')['logs'] == [] and get('/api/sent')['sent'] == []
        assert get('/api/recovery')['items'] == []
        current = get('/api/campaigns/current')
        assert current['state'] == 'idle' and current['events'] == [] and current['totals']['total_sends'] == 0
        if test_manifest:
            groups = ('metadata_json', 'narration', 'timesync_and_imu', 'recording_head_data_data_vrs')
            manifest = user_root / 'fixture-download-urls.json'
            manifest.write_text(json.dumps({'sequences': {'portable_sequence': {
                name: {'filename': name + '.zip', 'sha1sum': 'a' * 40, 'file_size_bytes': 100,
                       'download_url': 'https://fixture.fbcdn.net/' + name + '.zip'}
                for name in groups}}}), encoding='utf8')
            assert get('/api/library/nymeria/import', method='POST', body={'path':str(manifest)})['ok'] is True
            deadline = time.monotonic() + 15
            while True:
                operation = get('/api/library/nymeria/operation')
                if not operation['running']:
                    assert operation['state'] == 'done'
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError('Portable manifest import did not finish')
                time.sleep(.1)
            assert get('/api/library/nymeria/sequences')['total'] == 1
            assert get('/api/campaigns/current')['state'] == 'idle'
    finally:
        process.close()


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
        catalog_only = library / 'data/nymeria/catalog-only'
        catalog_only.mkdir(parents=True)
        (catalog_only / 'metadata.json').write_text(
            json.dumps({'uid': 'catalog-only', 'head_duration_sec': 600}), encoding='utf-8')
        customer = root / "customer-a"
        probe(service, customer, library, [])
        credentials = customer / "secrets/token_fixture.json"
        original = json.dumps({"email": "fixture@example.invalid", "idToken": "fixture-private-token"}).encode()
        credentials.write_bytes(original)
        probe(service, customer, library, ["fixture@example.invalid"])
        assert credentials.read_bytes() == original
        probe(service, root / "customer-b", library, [], test_manifest=True)
        portable_library = root / "relocated-library"
        shutil.copytree(library, portable_library, ignore=shutil.ignore_patterns('_catalog'))
        probe(service, root / "customer-c", portable_library, [], portable_setup=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({"passed": True, "checks": ["fresh_installation", "restart_preserves_credentials",
        "separate_customer_roots", "local_api_authentication", "empty_recovery", "no_campaign_started",
        "original_library_index_and_fts", "original_duration_and_unknown_sensor_state",
        "prepared_library_inventory", "prepared_library_authentication",
        "general_local_media_inventory", "owned_process_tree_shutdown",
        "nymeria_sdk_core_device_time", "nymeria_metadata_only_not_ready",
        "nymeria_source_catalog_and_packaged_current_tasks", "nymeria_source_catalog_authentication",
        "nymeria_manifest_import_without_external_python_or_sdk",
        "full_campaign_reset_preserves_credentials_and_library",
        "native_private_manifest_bootstrap_in_relocated_library",
        "native_catalog_setup_acquires_no_vrs_or_imu"]}, indent=2), encoding="utf-8")
    print("Packaged service checks passed; no uploads or withdrawals requested.")


if __name__ == "__main__":
    main()
