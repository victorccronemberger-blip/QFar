from __future__ import annotations

import os
import ssl
import unittest
from unittest import mock

from moneymin import tls


class PortableTlsTests(unittest.TestCase):
    def tearDown(self):
        tls.context.cache_clear()

    def test_context_loads_bundled_ca_file(self):
        fake_context = mock.Mock(spec=ssl.SSLContext)
        with mock.patch.object(tls.ssl, "create_default_context", return_value=fake_context), \
             mock.patch.object(tls, "ca_bundle", return_value=r"C:\QMoney\cacert.pem"):
            value = tls.context()

        self.assertIs(value, fake_context)
        fake_context.load_verify_locations.assert_called_once_with(
            cafile=r"C:\QMoney\cacert.pem")

    def test_environment_exposes_bundle_to_http_libraries(self):
        names = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
                 "AWS_CA_BUNDLE")
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(tls, "ca_bundle", return_value=r"C:\QMoney\cacert.pem"):
            tls.configure_environment()
            self.assertTrue(all(
                os.environ[name] == r"C:\QMoney\cacert.pem" for name in names))


if __name__ == "__main__":
    unittest.main()
