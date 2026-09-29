import unittest
from unittest.mock import patch
from moneymin.minute_api import Session, AuthError

class CatalogHttpTests(unittest.TestCase):
    def test_denied_and_failed_catalogs_are_not_empty_success(self):
        for status in (401,403,429,500):
            for method in ('categories','org_tasks','all_tasks'):
                session=Session({})
                with self.subTest(status=status,method=method), patch.object(session,'get',return_value=(status,'{"detail":"Request cannot be completed."}')):
                    with self.assertRaises(AuthError):
                        getattr(session,method)(*(() if method=='categories' else ('org',)))
    def test_invalid_payloads_fail_but_empty_lists_remain_valid(self):
        for payload in ('invalid','{}','{"tasks":null}','{"tasks":[1]}'):
            s=Session({})
            with self.subTest(payload=payload), patch.object(s,'get',return_value=(200,payload)):
                with self.assertRaises(AuthError): s.all_tasks('org')
        for payload in ('[]','{"tasks":[]}'):
            s=Session({})
            with patch.object(s,'get',return_value=(200,payload)):
                self.assertEqual(s.all_tasks('org'),[])
    def test_valid_catalog_rows_preserved(self):
        s=Session({})
        with patch.object(s,'get',return_value=(200,'{"tasks":[{"id":"a","name":"Task"}]}')):
            self.assertEqual(s.all_tasks('org'),[{'id':'a','name':'Task'}])
