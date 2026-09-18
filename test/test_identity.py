import re
import unittest
from unittest.mock import patch

from moneymin.identity import _OLD_CLUSTER, gerar_identidade, gerar_senha


class IdentityTests(unittest.TestCase):
    def test_batch_avoids_old_firstname_lastname_digits_cluster(self) -> None:
        seen: set[str] = set()
        shapes: set[str] = set()
        for _ in range(120):
            ident = gerar_identidade(domain="galatic.com.br", existentes=seen)
            seen.add(ident["email"])
            local = ident["email"].split("@", 1)[0]
            self.assertFalse(_OLD_CLUSTER.fullmatch(local), local)
            self.assertTrue(re.fullmatch(r"[a-z][a-z0-9._-]{1,28}[a-z0-9]", local), local)
            self.assertTrue(ident["email"].endswith("@galatic.com.br"))
            self.assertIn(ident["gender"], {"male", "female"})
            shapes.add(_shape(local))
        self.assertGreaterEqual(len(shapes), 5)
        self.assertEqual(len(seen), 120)

    def test_password_composition_accepts_any_random_prefix(self) -> None:
        for upper, lower, symbol in [("M", "m", "!"), ("Z", "a", "%")]:
            with self.subTest(prefix=upper + lower), \
                 patch("moneymin.identity.secrets.choice", side_effect=["x"] * 10 + [upper, lower, symbol]), \
                 patch("moneymin.identity.secrets.randbelow", return_value=7):
                password = gerar_senha()
            self.assertEqual(password, upper + lower + "x" * 10 + symbol + "17")


def _shape(local: str) -> str:
    if "." in local and "_" not in local and "-" not in local:
        return "dotted"
    if "_" in local:
        return "underscore"
    if "-" in local:
        return "hyphen"
    if re.fullmatch(r"[a-z]+\d{2}", local):
        return "name-year"
    if re.fullmatch(r"[a-z]+", local):
        return "letters"
    return "other"
