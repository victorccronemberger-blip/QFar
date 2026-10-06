"""Reject ambiguous encoded sidecars using declared offline parser fixtures."""
import hashlib
import io
import json
import unittest
import zipfile

from moneymin import upload, validate
from test_validate_metadata_contract import FRAMES, IMU, LOG_ID, native_fixture


def sidecar(raw):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{LOG_ID}.imu.csv", IMU)
        archive.writestr(f"{LOG_ID}.frames.csv", FRAMES)
        archive.writestr(f"{LOG_ID}.metadata.json", raw)
    return output.getvalue()


def invalid_metadata():
    valid = json.dumps(native_fixture())
    return {
        "duplicate_log_id": valid.replace(
            f'"logId": "{LOG_ID}"', f'"logId": "other-fixture_99", "logId": "{LOG_ID}"'),
        "duplicate_nested_width": valid.replace('"width": 1280', '"width": 640, "width": 1280'),
        "duplicate_unknown_key": valid[:-1] + ',"extension":1,"extension":2}',
        "escaped_duplicate_unknown_key": valid[:-1] + ',"extension":1,"extensi\\u006fn":2}',
        "nan_unknown_key": valid[:-1] + ',"extension":NaN}',
        "positive_infinity_unknown_key": valid[:-1] + ',"extension":Infinity}',
        "negative_infinity_unknown_key": valid[:-1] + ',"extension":-Infinity}',
        "overflow_unknown_key": valid[:-1] + ',"extension":1e999}',
        "negative_overflow_nested_list": valid[:-1] + ',"extension":{"values":[-1e999]}}',
        "non_object": "[]",
        "truncated_json": valid[:-1],
    }


class EncodedMetadataIntegrity(unittest.TestCase):
    def test_validator_rejects_ambiguity_at_any_depth_without_changing_bytes(self):
        for name, raw in invalid_metadata().items():
            with self.subTest(case=name):
                payload = sidecar(raw)
                before = hashlib.sha256(payload).digest()
                checks = validate.validate_sidecar_zip(payload, log_id=LOG_ID, duration_ms=1000)
                self.assertTrue(any(check.status == "fail" for check in checks))
                self.assertEqual(hashlib.sha256(payload).digest(), before)

    def test_upload_preflight_rejects_the_same_bytes_with_nontransient_error(self):
        for name, raw in invalid_metadata().items():
            with self.subTest(case=name):
                payload = sidecar(raw)
                before = hashlib.sha256(payload).digest()
                with self.assertRaises(upload.UploadError) as caught:
                    upload._validate_sidecar_zip(payload, log_id=LOG_ID, duration_ms=1000)
                self.assertFalse(caught.exception.transient)
                self.assertEqual(hashlib.sha256(payload).digest(), before)
                self.assertNotIn("other-fixture_99", str(caught.exception))

    def test_unique_finite_extensions_and_existing_identity_are_preserved(self):
        metadata = native_fixture()
        metadata["extension"] = {"values": [1.5, -2.0, 1e100], "label": "fixture-é"}
        payload = sidecar(json.dumps(metadata, ensure_ascii=False))
        before = hashlib.sha256(payload).digest()
        checks = validate.validate_sidecar_zip(payload, log_id=LOG_ID, duration_ms=1000)
        self.assertFalse(any(check.status == "fail" for check in checks))
        self.assertEqual(upload._validate_sidecar_zip(payload, log_id=LOG_ID, duration_ms=1000), metadata)
        self.assertEqual(hashlib.sha256(payload).digest(), before)

    def test_invalid_declared_duration_is_a_private_nontransient_preflight_error(self):
        for value in (True, "1000", "fixture-duration-secret", None, [], {}, [1], {"fixture": 1}, 1000.0, 0, -1):
            with self.subTest(value=value):
                metadata = native_fixture()
                metadata["durationMs"] = value
                payload = sidecar(json.dumps(metadata))
                before = hashlib.sha256(payload).digest()
                with self.assertRaises(upload.UploadError) as caught:
                    upload._validate_sidecar_zip(payload, log_id=LOG_ID, duration_ms=1000)
                self.assertFalse(caught.exception.transient)
                self.assertNotIn("fixture-duration-secret", str(caught.exception))
                self.assertEqual(hashlib.sha256(payload).digest(), before)
                checks = validate.validate_sidecar_zip(payload, log_id=LOG_ID, duration_ms=1000)
                self.assertTrue(any(check.status == "fail" for check in checks))
                self.assertNotIn("fixture-duration-secret", json.dumps([check.detail for check in checks]))
                checks = validate.validate_upload_meta(metadata, log_id=LOG_ID, duration_ms=1000)
                self.assertTrue(any(check.status == "fail" for check in checks))
                self.assertNotIn("fixture-duration-secret", json.dumps([check.detail for check in checks]))


if __name__ == "__main__":
    unittest.main()
