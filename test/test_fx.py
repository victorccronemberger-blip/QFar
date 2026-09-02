from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from moneymin import fx


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class ExchangeRateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cache = Path(self.temp.name) / "quote.json"
        self.patch = mock.patch.object(fx, "CACHE_PATH", self.cache)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_fetches_and_caches_bcb_quote(self):
        calls = []

        def opener(request, timeout):
            calls.append((request.full_url, timeout))
            return _Response([{"data": "01/09/2026", "valor": "5.1570"}])

        quote = fx.usd_brl_quote(now=1_788_300_000, opener=opener)
        self.assertEqual(quote["rate"], 5.157)
        self.assertEqual(quote["quote_date"], "2026-09-01")
        self.assertFalse(quote["stale"])
        self.assertEqual(len(calls), 1)

        cached = fx.usd_brl_quote(
            now=1_788_300_001,
            opener=lambda *_args, **_kwargs: self.fail("não deveria acessar a rede"),
        )
        self.assertEqual(cached["rate"], 5.157)

    def test_uses_stale_cache_when_offline(self):
        self.cache.write_text(json.dumps({
            "available": True,
            "rate": 5.1,
            "quote_date": "2026-08-31",
            "fetched_at": 100,
            "source": "Banco Central do Brasil",
        }), encoding="utf-8")

        def offline(*_args, **_kwargs):
            raise OSError("offline")

        quote = fx.usd_brl_quote(now=100 + fx.CACHE_TTL_S + 1, opener=offline)
        self.assertTrue(quote["available"])
        self.assertTrue(quote["stale"])
        self.assertEqual(quote["rate"], 5.1)

    def test_reports_unavailable_without_cache(self):
        def offline(*_args, **_kwargs):
            raise OSError("offline")

        quote = fx.usd_brl_quote(now=500, opener=offline)
        self.assertFalse(quote["available"])
        self.assertIsNone(quote["rate"])


if __name__ == "__main__":
    unittest.main()
