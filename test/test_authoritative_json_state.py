"""Strict state reads preserve ambiguity instead of producing empty state."""
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

from moneymin.atomic_io import JsonStateError, load_json, load_json_state


class AuthoritativeJsonStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "state.json"

    def test_only_missing_returns_default_without_writes(self):
        missing = {"missing": True}
        self.assertIs(load_json_state(self.path, missing), missing)
        self.assertEqual(list(self.root.iterdir()), [])
        self.path.write_text('{"large":9007199254740993,"nested":{"ok":true}}', encoding="utf-8-sig")
        before = self.path.read_bytes()
        value = load_json_state(self.path, {})
        self.assertEqual(value["large"], 9007199254740993)
        self.assertEqual(self.path.read_bytes(), before)

    def test_corrupt_ambiguous_or_nonobject_state_fails_privately(self):
        values = (b"\xff-private-canary", b'{"private-canary":', b'null', b'[]', b'false',
                  b'{"private-canary":1,"private-canary":2}', b'{"nested":{"a":1,"a":2}}',
                  b'{"number":NaN}', b'{"number":Infinity}', b'{"number":-Infinity}',
                  b'{"number":1e999}', b'{"number":-1e999}')
        for raw in values:
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                with self.assertRaises(JsonStateError) as raised:
                    load_json_state(self.path, {})
                self.assertNotIn("private-canary", str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)
                self.assertTrue(raised.exception.__suppress_context__)
                self.assertEqual(self.path.read_bytes(), raw)

    def test_read_error_is_private_and_preserves_state(self):
        self.path.write_bytes(b'{"ok":true}')
        with patch.object(Path, "read_text", side_effect=PermissionError("private-canary")):
            try:
                load_json_state(self.path, {})
            except JsonStateError:
                diagnostic = traceback.format_exc()
            else:
                self.fail("read failure accepted")
        self.assertNotIn("private-canary", diagnostic)
        self.assertEqual(self.path.read_bytes(), b'{"ok":true}')

    def test_list_shape_is_explicit_and_cache_policy_remains_available(self):
        self.path.write_bytes(b'[1,2]')
        self.assertEqual(load_json_state(self.path, [], expected_type=list), [1, 2])
        self.path.write_bytes(b'{"broken":')
        self.assertEqual(load_json(self.path, {"cache_miss": True}), {"cache_miss": True})
        with self.assertRaises(JsonStateError):
            load_json_state(self.path, [])


if __name__ == "__main__":
    unittest.main()
