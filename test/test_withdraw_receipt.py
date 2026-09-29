import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from moneymin.web import server
from moneymin.atomic_io import save_json

class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.p=patch.object(server.config,'DATA_DIR',self.root)
        self.p.start(); self.addCleanup(self.p.stop)
        self.email='fixture@example.com'
    def record(self,status='ok',**extra):
        save_json(self.root/'withdraw_last_result.json',{'email':self.email,'finished_at':1790695166,
            'result':{'status':status,**extra}})
    def test_accepted_is_independent_of_cleanup(self):
        self.record(cleanupPending=True,wiseDestinationRemoved=False,payoutPreferenceRestored=True)
        save_json(self.root/'wise_cleanup_pending.json',{'pending':True,'email':self.email})
        r=server._last_withdrawal_receipt({self.email})
        self.assertTrue(r['accepted']); self.assertTrue(r['cleanup_pending'])
        self.assertIn('aceito pela Crowtado',r['message'])
        self.assertIn('recebimento na Wise não foi verificado',r['message'])
        self.assertIn('bloqueados',r['message'])
    def test_old_cleanup_evidence_does_not_invent_current_failure_or_settlement(self):
        self.record(cleanupPending=True)
        save_json(self.root/'wise_cleanup_pending.json',{'pending':False})
        r=server._last_withdrawal_receipt({self.email})
        self.assertTrue(r['accepted']); self.assertFalse(r['cleanup_pending'])
        self.assertIn('Houve pendência',r['message'])
        self.assertNotIn('Dots confirmado',r['message'])
    def test_unknown_and_review_are_not_accepted(self):
        for status in ('unknown','review_required','manual_failed','not_requested'):
            self.record(status)
            self.assertFalse(server._last_withdrawal_receipt({self.email})['accepted'])
    def test_removed_account_and_invalid_records_hidden(self):
        self.record()
        self.assertEqual(server._last_withdrawal_receipt(set()),{})
        save_json(self.root/'withdraw_last_result.json',{'email':self.email,'result':{'status':'ok'},'finished_at':'bad'})
        self.assertEqual(server._last_withdrawal_receipt({self.email}),{})
    def test_secrets_are_not_exposed(self):
        self.record(token='secret',destination_email='private@example.com')
        self.assertNotIn('secret',str(server._last_withdrawal_receipt({self.email})))
        self.assertNotIn('private@example.com',str(server._last_withdrawal_receipt({self.email})))
