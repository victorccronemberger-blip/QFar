import unittest

from moneymin import upload


class UploadCsvIntegrityTests(unittest.TestCase):
    def span(self, payload):
        return upload._csv_span_ns(payload, "t", ("t", "ax"))

    def test_timestamps_above_float_precision_are_preserved(self):
        start = 2**53 + 3
        payload = f"t,ax\n{start},0\n{start + 1},0\n{start + 2},0\n".encode()
        self.assertEqual(self.span(payload), (start, start + 2, 3))

    def test_middle_rows_are_checked_before_transport(self):
        for payload in (
            b"t,ax\n1,0\n5,0\n4,0\n10,0\n",
            b"t,ax\n1,0\n1,0\n10,0\n",
            b"t,ax\n1,0\ninvalid,0\n10,0\n",
            b"t,ax\n1,0\n2\n10,0\n",
            b"t,t,ax\n1,1,0\n2,2,0\n",
            b"t,ax\n1,0\n9223372036854775808,0\n",
        ):
            with self.subTest(payload=payload), self.assertRaises(upload.UploadError):
                self.span(payload)
