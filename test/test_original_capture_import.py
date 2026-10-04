"""Literal, unacquired capture fixtures; no real files or provider calls."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import traceback
import unittest
import warnings
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("_capture_import_fixture", PROJECT / "moneymin/capture_import.py")
capture = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = capture
SPEC.loader.exec_module(capture)
LOG_ID = "literal-session_0"
IMU = b"t,ax,ay,az,wx,wy,wz\n9007199254740993,0.1,-0.2,9.8,0,0,0\n9007199254740994,0,0,9.8,0,0,0\n"
FRAMES = b"i,ptsNs,dtNs,tNs,key\n0,1234,0,9007199254740993,1\n1,2000,766,9007199254740994,0\n"


def metadata():
    return {
        "id": LOG_ID, "logId": LOG_ID,
        "session": {"id": "literal-session"},
        "chunk": {"index": 0, "startTimeMs": 1790985600000, "endTimeMs": 1790985601000},
        "platform": {"os": "android", "version": 34},
        "timebase": {"clockDomain": "literal-fixture-clock", "startNs": "9007199254740993",
                     "endNs": "9007199254740994", "hostStartElapsedNs": "9007199254740000"},
        "device": {"model": "fixture-only-not-acquired"},
        "video": {"path": "/literal-source/not-decoded.mp4", "width": 10, "height": 10,
                  "rotationDeg": 0},
        "source": "ego", "durationMs": 1000, "appVersion": "1.28.0",
        "createdAt": "2026-10-03T00:00:00.000Z",
        "artifacts": [{"name": kind, "remoteFilename": f"{LOG_ID}.{kind}.csv"}
                      for kind in ("imu", "frames")],
        "literal_fixture_only": True,
    }


def archive_bytes(value=None, *, raw_metadata=None, imu=IMU, frames=FRAMES, extras=(),
                  compression=zipfile.ZIP_DEFLATED):
    output = io.BytesIO()
    raw_metadata = (json.dumps(metadata() if value is None else value).encode("utf-8")
                    if raw_metadata is None else raw_metadata)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(output, "w", compression) as archive:
            archive.writestr(f"{LOG_ID}.metadata.json", raw_metadata)
            archive.writestr(f"{LOG_ID}.imu.csv", imu)
            archive.writestr(f"{LOG_ID}.frames.csv", frames)
            for name, content in extras:
                archive.writestr(name, content)
    return output.getvalue()


class OriginalCaptureImportTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="qmoney-original-capture-fixture-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.media = self.root / "original media ç.mp4"
        self.sidecar = self.root / "original sidecar ç.data.zip"
        self.media.write_bytes(b"literal-media-not-decoded-no-physical-acquisition")
        self.sidecar.write_bytes(archive_bytes())
        self.canary = "artificial-identity-or-secret-canary"

    def inspect(self, **kwargs):
        return capture.inspect_original_capture(self.media, self.sidecar, **kwargs)

    def rejects(self, data, code=None, **kwargs):
        self.sidecar.write_bytes(data)
        originals = {path: path.read_bytes() for path in (self.media, self.sidecar)}
        with self.assertRaises(capture.CaptureImportError) as raised:
            self.inspect(**kwargs)
        if code:
            self.assertEqual(raised.exception.code, code)
        self.assertNotIn(self.canary, "".join(traceback.format_exception(raised.exception)))
        self.assertNotIn(str(self.root), str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        for path, before in originals.items():
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(set(self.root.iterdir()), set(originals))

    def test_descriptor_preserves_identity_metadata_paths_bytes_and_hashes(self):
        before = {path: path.read_bytes() for path in (self.media, self.sidecar)}
        with patch.object(zipfile.ZipFile, "extractall", side_effect=AssertionError("never extract")), \
                patch.object(zipfile.ZipFile, "extract", side_effect=AssertionError("never extract")), \
                patch.object(socket.socket, "connect", side_effect=AssertionError("never connect")), \
                patch.object(subprocess, "Popen", side_effect=AssertionError("never probe")):
            result = self.inspect()
        self.assertEqual(result.metadata, metadata())
        self.assertEqual(result.metadata["timebase"]["startNs"], "9007199254740993")
        self.assertEqual((result.imu_rows, result.frames_rows), (2, 2))
        for evidence in (result.media, result.sidecar):
            self.assertEqual(evidence.bytes, len(before[evidence.path]))
            self.assertEqual(evidence.sha256, hashlib.sha256(before[evidence.path]).hexdigest())
            self.assertEqual(evidence.path.read_bytes(), before[evidence.path])
        with zipfile.ZipFile(io.BytesIO(before[self.sidecar])) as archive:
            for member in result.members:
                self.assertEqual(member.sha256, hashlib.sha256(archive.read(member.name)).hexdigest())
        self.assertEqual(set(self.root.iterdir()), {self.media, self.sidecar})

    def test_public_summary_excludes_private_metadata_identity_and_paths(self):
        value = metadata()
        value["password"] = self.canary
        value["owner"] = {"email": self.canary}
        self.sidecar.write_bytes(archive_bytes(value))
        result = self.inspect()
        summary = result.public_summary()
        public = json.dumps(summary)
        for private in (self.canary, LOG_ID, "literal-session", str(self.root), "owner", "password"):
            self.assertNotIn(private, public)
        self.assertEqual(result.metadata["password"], self.canary)
        self.assertTrue(summary["valid_structure"])
        self.assertFalse(summary["physical_provenance_verified"])
        self.assertFalse(summary["provider_acceptance_verified"])
        self.assertFalse(summary["media_decoding_verified"])
        self.assertFalse(summary["csv_temporal_consistency_verified"])
        self.assertEqual(summary["validation_policy"], "safety_container_identity_csv_types")

    def test_strict_json_duplicate_keys_nonfinite_and_invalid_root(self):
        for raw in (b'{"id":"x","id":"y"}', b'{"nested":{"x":1,"x":2}}',
                    b'{"value":NaN}', b'{"value":Infinity}', b'{"value":-Infinity}',
                    b'{"value":1e999}', b'[]', b'null', b'{', b'\xff'):
            with self.subTest(raw=raw):
                self.rejects(archive_bytes(raw_metadata=raw), "invalid_metadata")

    def test_metadata_identity_links_are_consistent_without_relabelling(self):
        variants = []
        for field, value in (("id", self.canary), ("logId", self.canary),
                             ("sessionId", self.canary), ("chunkIndex", True),
                             ("chunk_index", 1)):
            item = metadata()
            item[field] = value
            variants.append(item)
        item = metadata()
        item["session"]["id"] = "other-session"
        variants.append(item)
        item = metadata()
        item["chunk"]["index"] = 1
        variants.append(item)
        item = metadata()
        item["artifacts"][0]["remoteFilename"] = "different.imu.csv"
        variants.append(item)
        for item in variants:
            with self.subTest(keys=tuple(item)):
                self.rejects(archive_bytes(item), "inconsistent_identity")

    def test_metadata_types_reject_bool_float_or_missing_clock_identifiers(self):
        variants = []
        for field in ("session", "chunk", "platform", "timebase"):
            item = metadata()
            item[field] = []
            variants.append(item)
        for section, field, value in (("chunk", "index", True), ("chunk", "index", 0.0),
                                       ("chunk", "index", -1), ("chunk", "index", 2**31),
                                       ("chunk", "startTimeMs", True),
                                       ("platform", "version", True),
                                       ("platform", "version", 0),
                                       ("timebase", "clockDomain", ""),
                                       ("timebase", "startNs", 1),
                                       ("timebase", "startNs", str(2**63))):
            item = metadata()
            item[section][field] = value
            variants.append(item)
        for item in variants:
            with self.subTest(keys=tuple(item)):
                self.rejects(archive_bytes(item), "invalid_metadata")

    def test_unknown_platform_clock_and_owner_are_preserved_without_inference(self):
        value = metadata()
        value["platform"] = {"type": "external-original-platform", "version": 1}
        value["timebase"]["clockDomain"] = "unverified-original-domain"
        value["owner"] = {"originalId": self.canary}
        value["timebase"]["startNs"] = "0000000000000000123"
        self.sidecar.write_bytes(archive_bytes(value))
        self.assertEqual(self.inspect().metadata, value)

    def test_missing_required_csv_or_metadata_is_rejected(self):
        with zipfile.ZipFile(io.BytesIO(archive_bytes())) as original:
            members = {name: original.read(name) for name in original.namelist()}
        for excluded in members:
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w") as archive:
                for name, value in members.items():
                    if name != excluded:
                        archive.writestr(name, value)
                archive.writestr("extra-fixture.txt", "explicit-extra")
            with self.subTest(excluded=excluded):
                self.rejects(output.getvalue())

    def test_duplicate_and_case_colliding_members_are_rejected(self):
        for name in (f"{LOG_ID}.imu.csv", f"{LOG_ID}.imu.csv".upper()):
            with self.subTest(name=name):
                self.rejects(archive_bytes(extras=((name, IMU),)), "unsafe_archive")

    def test_unsafe_member_paths_and_windows_names_are_rejected(self):
        for name in ("../escape", "/absolute", "folder/inside", "folder\\inside", "C:ads",
                     "CON", "NUL.txt", "COM1.dat", "trailing.", "trailing ", ".", ".."):
            with self.subTest(name=name):
                self.rejects(archive_bytes(extras=((name, b"fixture"),)), "unsafe_archive")

    def test_symlink_reparse_and_directory_members_are_rejected(self):
        for mode, attr, name in ((stat.S_IFLNK | 0o777, 0, "link"),
                                 (0, 0x400, "junction"),
                                 (stat.S_IFDIR | 0o755, 0, "directory/")):
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (mode << 16) | attr
            with self.subTest(name=name):
                self.rejects(archive_bytes(extras=((info, b"fixture"),)), "unsafe_archive")

    def test_encrypted_flags_rejected_before_member_decompression(self):
        data = bytearray(archive_bytes())
        local = data.find(b"PK\x03\x04")
        central = data.find(b"PK\x01\x02")
        struct.pack_into("<H", data, local + 6, struct.unpack_from("<H", data, local + 6)[0] | 1)
        struct.pack_into("<H", data, central + 8, struct.unpack_from("<H", data, central + 8)[0] | 1)
        with patch.object(zipfile.ZipFile, "open", side_effect=AssertionError("do not decrypt")):
            self.rejects(bytes(data), "unsafe_archive")

    def test_declared_and_actual_central_directory_counts_are_bounded(self):
        for declared in (0xffff, 33):
            data = bytearray(archive_bytes())
            eocd = data.rfind(b"PK\x05\x06")
            struct.pack_into("<HH", data, eocd + 8, declared, declared)
            with self.subTest(declared=declared), \
                    patch.object(zipfile, "ZipFile", side_effect=AssertionError("bounded before parse")):
                self.rejects(bytes(data))
        data = bytearray(archive_bytes(extras=tuple((f"extra-{i}", b"x") for i in range(40))))
        eocd = data.rfind(b"PK\x05\x06")
        struct.pack_into("<HH", data, eocd + 8, 3, 3)
        with patch.object(zipfile, "ZipFile", side_effect=AssertionError("bounded before parse")):
            self.rejects(bytes(data), "limit_exceeded")

    def test_crc_corruption_is_detected_without_writing(self):
        data = bytearray(archive_bytes(compression=zipfile.ZIP_STORED))
        start = data.find(IMU)
        self.assertGreater(start, 0)
        data[start] ^= 1
        self.rejects(bytes(data), "invalid_archive")

    def test_zip64_local_headers_and_unsupported_compression_are_rejected(self):
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(archive_bytes())) as original, \
                zipfile.ZipFile(output, "w") as archive:
            for name in original.namelist():
                with archive.open(name, "w", force_zip64=True) as member:
                    member.write(original.read(name))
        self.rejects(output.getvalue(), "invalid_archive")
        self.rejects(archive_bytes(compression=zipfile.ZIP_BZIP2), "invalid_archive")

    def test_zip_bomb_and_explicit_size_limits_are_rejected(self):
        self.rejects(archive_bytes(extras=(("compressed-bomb-fixture", b"A" * 100000),)), "limit_exceeded")
        base = capture.CaptureLimits()
        for field, value in (("archive_bytes", 10), ("media_bytes", 10),
                             ("metadata_bytes", 10), ("member_bytes", 10),
                             ("expanded_bytes", 10), ("csv_rows", 1), ("csv_line_bytes", 10)):
            with self.subTest(field=field):
                self.rejects(archive_bytes(), "limit_exceeded", limits=replace(base, **{field: value}))

    def test_invalid_limits_are_not_silently_defaulted(self):
        for limits in (False, {}, replace(capture.CaptureLimits(), members=True),
                       replace(capture.CaptureLimits(), compression_ratio=0)):
            with self.subTest(type=type(limits).__name__):
                with self.assertRaises(capture.CaptureImportError) as raised:
                    self.inspect(limits=limits)
                self.assertEqual(raised.exception.code, "invalid_arguments")

    def test_invalid_csv_schema_values_and_empty_samples_are_rejected(self):
        for imu in (b"bad-header\n", IMU.replace(b"0.1", b"NaN"),
                    IMU.replace(b"0.1", b"Infinity"), IMU.replace(b"0.1", b"bad"),
                    IMU.replace(b"9007199254740993", b"9223372036854775808"),
                    b"t,ax,ay,az,wx,wy,wz\n", IMU.replace(b"0.1", b"\xff")):
            with self.subTest(imu=imu[:30]):
                self.rejects(archive_bytes(imu=imu), "invalid_csv")
        for frames in (b"i,ptsNs,dtNs,tNs,key\n", FRAMES.replace(b",1\n", b",true\n"),
                       FRAMES.replace(b"0,1234", b"0.0,1234"),
                       FRAMES.replace(b"0,1234", b"-1,1234")):
            with self.subTest(frames=frames[:30]):
                self.rejects(archive_bytes(frames=frames), "invalid_csv")

    def test_source_mutation_during_streaming_hash_is_rejected(self):
        original = capture._file_hash
        first = True
        def changed(stream, limit):
            nonlocal first
            result = original(stream, limit)
            if first:
                first = False
                with self.media.open("ab") as output:
                    output.write(b"changed-by-explicit-fixture")
            return result
        with patch.object(capture, "_file_hash", side_effect=changed):
            with self.assertRaises(capture.CaptureImportError) as raised:
                self.inspect()
        self.assertEqual(raised.exception.code, "source_changed")

    def test_missing_nonregular_and_unc_sources_are_rejected_privately(self):
        for media in (self.root / self.canary, self.root, "\\\\fixture-server\\capture.mp4"):
            with self.subTest(kind=type(media).__name__):
                with self.assertRaises(capture.CaptureImportError) as raised:
                    capture.inspect_original_capture(media, self.sidecar)
                self.assertNotIn(self.canary, "".join(traceback.format_exception(raised.exception)))

    def test_cli_is_read_only_and_outputs_minimum_json(self):
        value = metadata()
        value["password"] = self.canary
        self.sidecar.write_bytes(archive_bytes(value))
        before = {path: path.read_bytes() for path in (self.media, self.sidecar)}
        result = subprocess.run([sys.executable, str(PROJECT / "scripts/inspect_original_capture.py"),
                                 "--media", str(self.media), "--sidecar", str(self.sidecar), "--json"],
                                cwd=self.root, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertTrue(summary["valid_structure"])
        self.assertFalse(summary["physical_provenance_verified"])
        for private in (self.canary, LOG_ID, str(self.root), "password"):
            self.assertNotIn(private, result.stdout + result.stderr)
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertEqual(set(self.root.iterdir()), set(before))

    def test_cli_validation_failure_is_fixed_private_json(self):
        self.sidecar.write_bytes(archive_bytes(raw_metadata=b'{"password":"' + self.canary.encode() + b'",'))
        result = subprocess.run([sys.executable, str(PROJECT / "scripts/inspect_original_capture.py"),
                                 "--media", str(self.media), "--sidecar", str(self.sidecar), "--json"],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(json.loads(result.stdout)["valid_structure"])
        self.assertNotIn(self.canary, result.stdout + result.stderr)
        self.assertNotIn(str(self.root), result.stdout + result.stderr)

    def test_isolated_import_reads_no_product_configuration_or_private_state(self):
        code = (
            "import importlib.util,sys\n"
            "from pathlib import Path\n"
            "from unittest.mock import patch\n"
            f"path=Path({str(PROJECT / 'moneymin/capture_import.py')!r})\n"
            "spec=importlib.util.spec_from_file_location('capture_only',path)\n"
            "module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module\n"
            "with patch.object(Path,'read_bytes',side_effect=AssertionError('no reads')),"
            "patch.object(Path,'read_text',side_effect=AssertionError('no reads')):\n"
            " spec.loader.exec_module(module)\n"
            "assert not any(name=='moneymin' or name.startswith('moneymin.') for name in sys.modules)\n"
            "print('pure import')\n"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                text=True, timeout=20, cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "pure import")

    def test_conventional_package_import_is_lazy_without_config_or_generators(self):
        code = (
            "import sys\n"
            "from pathlib import Path\n"
            "from unittest.mock import patch\n"
            "with patch.object(Path,'read_bytes',side_effect=AssertionError('no state reads')),"
            "patch.object(Path,'read_text',side_effect=AssertionError('no state reads')):\n"
            " import moneymin.capture_import as capture\n"
            "for name in ('moneymin.config','moneymin.sidecar','moneymin.campaign',"
            "'moneymin.minute_api','moneymin.upload','moneymin.secure_store'):\n"
            " assert name not in sys.modules,name\n"
            "assert callable(capture.inspect_original_capture)\n"
            "print('lazy package import')\n"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                text=True, timeout=20, cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "lazy package import")
