"""Protected ambiguous state and invalid section types cannot be overwritten."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moneymin import secure_store


class SecureStateAmbiguityTests(unittest.TestCase):
    @staticmethod
    def crypt(payload, *, protect):
        return b"fixture:" + payload if protect else payload.removeprefix(b"fixture:")

    def test_ambiguous_protected_json_blocks_read_and_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "vault"
            for raw in (b'{"private-canary":1,"private-canary":2}', b'{"number":NaN}', b'{"number":1e999}'):
                with self.subTest(raw=raw), patch.object(secure_store, "_crypt", side_effect=self.crypt):
                    original = self.crypt(raw, protect=True)
                    path.write_bytes(original)
                    for action in (lambda: secure_store.load_secure_settings(path, strict=True),
                                   lambda: secure_store.save_secure_settings(path, {"replacement": True})):
                        with self.assertRaises(secure_store.SecureStoreError) as raised:
                            action()
                        self.assertNotIn("private-canary", str(raised.exception))
                        self.assertEqual(path.read_bytes(), original)

    def test_invalid_section_input_fails_before_storage_or_deletion(self):
        for section, value in (("hostinger", False), ("hostinger", []), ("hostinger", 0),
                               (None, {}), ("", {}), ("schema", {}), ("bad\x00key", {})):
            with self.subTest(section=section), patch.object(secure_store, "load_secure_settings") as read, \
                 patch.object(secure_store, "save_secure_settings") as save:
                with self.assertRaises(secure_store.SecureStoreError):
                    secure_store.update_secure_section(Path("not-read"), section, value)
                read.assert_not_called()
                save.assert_not_called()

    def test_explicit_none_can_remove_section_and_dict_can_update(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(secure_store, "_crypt", side_effect=self.crypt):
            path = Path(folder) / "vault"
            secure_store.update_secure_section(path, "hostinger", {"token": "artificial"})
            self.assertEqual(secure_store.load_secure_settings(path, strict=True)["hostinger"], {"token": "artificial"})
            secure_store.update_secure_section(path, "hostinger", None)
            self.assertNotIn("hostinger", secure_store.load_secure_settings(path, strict=True))


if __name__ == "__main__":
    unittest.main()
