import unittest
from unittest import mock

from moneymin import config, org_policy


HUB = config.HUB_ORG_KEY
CROW = config.ORG_KEY
CLARU = config.CLARU_ORG_KEY


def orgs(*keys: str) -> list[dict]:
    return [{"name": key, "resourceKey": key} for key in keys]


class OrgPolicyTests(unittest.TestCase):
    def test_kind_by_domain(self) -> None:
        self.assertEqual(org_policy.account_kind("a@academy4u.com.br"), "crowtado")
        self.assertEqual(org_policy.account_kind("b@galatic.com.br"), "crowtado")
        self.assertEqual(org_policy.account_kind("c@cyara.com.br"), "crowtado")
        self.assertEqual(org_policy.account_kind("x@supply.claru.ai"), "claru")
        self.assertEqual(org_policy.account_kind("X@Supply.Claru.AI"), "claru")

    def test_crowtado_prefers_datoric_over_hub(self) -> None:
        email = "paulo_lima@galatic.com.br"
        self.assertEqual(org_policy.pick_org_key(email, orgs(HUB, CROW)), CROW)
        self.assertIsNone(org_policy.pick_org_key(email, orgs(HUB)))

    def test_claru_keeps_claru_even_if_also_in_datoric(self) -> None:
        email = "4wp6e4zg@supply.claru.ai"
        self.assertEqual(org_policy.pick_org_key(email, orgs(CLARU, CROW)), CLARU)
        self.assertNotEqual(org_policy.target_invite(email), config.INVITE_CODE)

    def test_ensure_joins_crowtado_when_datoric_missing(self) -> None:
        email = "sbarros@academy4u.com.br"
        sess = mock.Mock()
        sess.join_org.return_value = (200, "{}")
        sess.me.return_value = {"organizations": orgs(HUB, CROW)}
        key = org_policy.ensure_membership(sess, email, orgs(HUB))
        self.assertEqual(key, CROW)
        sess.join_org.assert_called_once_with(config.INVITE_CODE)

    def test_ensure_does_not_join_claru_into_crowtado(self) -> None:
        email = "7pzb35xh@supply.claru.ai"
        sess = mock.Mock()
        key = org_policy.ensure_membership(sess, email, orgs(CLARU, CROW))
        self.assertEqual(key, CLARU)
        sess.join_org.assert_not_called()

    def test_ensure_rejoins_claru_if_only_datoric_is_present(self) -> None:
        email = "7pzb35xh@supply.claru.ai"
        sess = mock.Mock()
        sess.join_org.return_value = (200, "{}")
        sess.me.return_value = {"organizations": orgs(CROW, CLARU)}
        key = org_policy.ensure_membership(sess, email, orgs(CROW))
        self.assertEqual(key, CLARU)
        sess.join_org.assert_called_once_with(config.CLARU_INVITE_CODE)
