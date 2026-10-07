import tempfile
import threading
import unittest
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from moneymin.atomic_io import load_json, save_bytes, save_json, _windows_replace_file


class AtomicIOTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'win32', 'Windows file metadata contract')
    def test_native_windows_replace_preserves_existing_named_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'state.json'
            source = Path(directory) / 'replacement.tmp'
            target.write_bytes(b'original')
            stream = Path(str(target) + ':qmoney-test')
            stream.write_bytes(b'preserved metadata')
            source.write_bytes(b'replacement')
            _windows_replace_file(source, target)
            self.assertEqual(target.read_bytes(), b'replacement')
            self.assertEqual(stream.read_bytes(), b'preserved metadata')
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_windows_access_denied_uses_metadata_preserving_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_bytes(b'original')
            denied = PermissionError('blocked replacement')
            denied.winerror = 5
            original = Path.replace
            with patch('moneymin.atomic_io.sys.platform', 'win32'), \
                 patch.object(Path, 'replace', side_effect=denied), \
                 patch('moneymin.atomic_io._windows_replace_file', side_effect=lambda source, target: original(source, target)) as native:
                save_bytes(path, b'replacement')
            native.assert_called_once()
            self.assertEqual(path.read_bytes(), b'replacement')
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_failed_native_replace_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_bytes(b'original')
            denied = PermissionError('denied')
            denied.winerror = 5
            with patch('moneymin.atomic_io.sys.platform', 'win32'), \
                 patch.object(Path, 'replace', side_effect=denied), \
                 patch('moneymin.atomic_io._windows_replace_file', side_effect=denied):
                with self.assertRaises(PermissionError):
                    save_bytes(path, b'replacement')
            self.assertEqual(path.read_bytes(), b'original')
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_disk_flush_failure_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_bytes(b"original")
            with patch("moneymin.atomic_io.os.fsync", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    save_json(path, {"new": True})
            self.assertEqual(path.read_bytes(), b"original")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_invalid_encoding_returns_default_without_overwriting_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            original = b'{"state": "\xff"}'
            path.write_bytes(original)
            fallback = {"state": "idle"}
            self.assertIs(load_json(path, fallback), fallback)
            self.assertEqual(path.read_bytes(), original)

    def test_utf8_with_or_without_bom_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            for encoding in ("utf-8", "utf-8-sig"):
                with self.subTest(encoding=encoding):
                    path.write_bytes('{"state": "migração"}'.encode(encoding))
                    self.assertEqual(load_json(path, {}), {"state": "migração"})

    def test_concurrent_writers_do_not_share_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            barrier = threading.Barrier(2)
            original = Path.replace
            sources = []

            def synchronized_replace(source, target):
                if source not in sources:
                    sources.append(source)
                    barrier.wait(timeout=5)
                return original(source, target)

            with patch.object(Path, "replace", synchronized_replace), ThreadPoolExecutor(2) as pool:
                futures = [pool.submit(save_json, path, {"writer": index}) for index in range(2)]
                for future in futures:
                    future.result(timeout=10)
            self.assertEqual(len(set(sources)), 2)
            self.assertIn(load_json(path, {})["writer"], (0, 1))
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_failed_replace_preserves_original_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.bin"
            path.write_bytes(b"original")
            with patch.object(Path, "replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    save_bytes(path, b"replacement")
            self.assertEqual(path.read_bytes(), b"original")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_serialization_failure_does_not_modify_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            save_json(path, {"original": True})
            with self.assertRaises(TypeError):
                save_json(path, object())
            self.assertEqual(load_json(path, {}), {"original": True})
            self.assertEqual(list(Path(directory).iterdir()), [path])
