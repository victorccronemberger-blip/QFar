"""Durable local admission and leases using inert, isolated fixture state."""
from pathlib import Path
from contextlib import ExitStack
import json
import tempfile
import threading
import subprocess
import sys
import unittest
from unittest.mock import patch

from moneymin import config, campaign_start_store as store
from moneymin.operation_lease import operation_lease, OperationLeaseError
from moneymin.web import server
import test_campaign_original_api as original_fixture
import test_campaign_end_to_end as dataset_fixture
START=original_fixture.START

class DurableStartTests(unittest.TestCase):
    def setUp(self):
        self.f=original_fixture.OriginalCampaignApiTests('test_missing_feature_reports_as_normal_contract_failure_not_collection_error')
        self.f.setUp();self.addCleanup(self.f.doCleanups)
    def test_lookup_after_restart_is_authenticated_pathless_and_does_not_start(self):
        f=self.f;review=f.review();self.assertEqual(f.post(START,f.start_body(review)).status_code,200)
        f.client=server.create_app(for_testing=True).test_client()
        url='/api/campaigns/starts/'+f.body['start_request_id']
        self.assertEqual(f.client.get(url).status_code,401)
        reply=f.client.get(url,headers=f.headers)
        self.assertEqual(reply.status_code,200);data=reply.get_json()
        self.assertEqual(data['status'],'admitted');self.assertFalse(data['may_start'])
        self.assertEqual(data['start_request_id'],f.body['start_request_id'])
        self.assertNotIn(str(f.root),json.dumps(data));f.runner.start.assert_called_once()
    def test_unknown_and_not_found_do_not_grant_restart_permission(self):
        f=self.f;review=f.review();f.runner.start.side_effect=RuntimeError('inert post-admission lost ack')
        self.assertEqual(f.post(START,f.start_body(review)).status_code,500)
        for uid,status in ((f.body['start_request_id'],'review'),('never-admitted','not_found')):
            reply=f.client.get('/api/campaigns/starts/'+uid,headers=f.headers)
            self.assertEqual(reply.status_code,200);self.assertEqual(reply.get_json()['status'],status)
            self.assertFalse(reply.get_json()['may_start'])
        f.runner.start.assert_called_once()
    def test_claim_precedes_runner_and_ack_failure_never_restarts(self):
        f=self.f;review=f.review()
        def accepted(cfg):
            row=store.lookup(cfg.start_request_id)
            self.assertEqual(row['phase'],'claimed');self.assertEqual(row['bindings']['session_id'],cfg.original_capture_plan.session_id)
            self.assertEqual(row['bindings']['expected_chunk_count'],2)
        f.runner.start.side_effect=accepted
        with patch.object(store,'acknowledge',side_effect=OSError('private inert write failure')):
            self.assertEqual(f.post(START,f.start_body(review)).status_code,500)
        f.client=server.create_app(for_testing=True).test_client()
        self.assertEqual(f.post(START,f.start_body(review)).status_code,500)
        f.runner.start.assert_called_once()
    def test_malformed_store_is_preserved_and_get_is_private(self):
        path=config.DATA_DIR/'start_requests.json';path.parent.mkdir(parents=True,exist_ok=True)
        for raw in (b'[]',b'{"version":true,"requests":{}}',b'{"version":1,"requests":{"bad":{}}}',b'{"version":1,"requests":NaN}'):
            path.write_bytes(raw)
            reply=self.f.client.get('/api/campaigns/starts/fixture-id',headers=self.f.headers)
            self.assertEqual(reply.status_code,409);self.assertNotIn(str(self.f.root),json.dumps(reply.get_json()))
            self.assertEqual(path.read_bytes(),raw)
        self.f.runner.start.assert_not_called()
    def test_dataset_claim_is_idempotent_and_conflicting_digest_is_rejected(self):
        request={'accounts':['owner@example.test'],'tasks':['task-fixture']}
        bindings={'accounts':[{'email':'owner@example.test','org_key':'org-fixture'}],'tasks':['task-fixture']}
        claimed,_=store.claim('dataset-fixture',kind='dataset',receipt_id='dataset-fixture',body=request,bindings=bindings)
        self.assertTrue(claimed)
        claimed,row=store.claim('dataset-fixture',kind='dataset',receipt_id='dataset-fixture',body=request,bindings=bindings)
        self.assertFalse(claimed);self.assertEqual(row['phase'],'claimed')
        with self.assertRaises(store.StartConflictError):
            store.claim('dataset-fixture',kind='dataset',receipt_id='dataset-fixture',body={**request,'count':2},bindings=bindings)
        store.acknowledge('dataset-fixture',{'ok':True,'total_sends':1,'start_request_id':'dataset-fixture'})
        self.assertEqual(store.lookup('dataset-fixture')['phase'],'admitted')
    def test_invalid_claim_is_rejected_before_publishing(self):
        path=config.DATA_DIR/'start_requests.json'
        with self.assertRaises(store.StartStoreError):
            store.claim('bad',kind='dataset',receipt_id='bad',body={'count':1},bindings={'accounts':[],'tasks':[]})
        self.assertFalse(path.exists())
    def test_same_thread_nested_lease_and_other_thread_exclusion(self):
        path=config.DATA_DIR/'fixture.lock';out=[]
        def competing():
            try:
                with operation_lease(path):out.append('incorrectly-acquired')
            except OperationLeaseError:out.append('blocked')
        with operation_lease(path):
            with operation_lease(path):pass
            t=threading.Thread(target=competing);t.start();t.join(2)
            self.assertFalse(t.is_alive());self.assertEqual(out,['blocked'])
        with operation_lease(path):pass
        self.assertTrue(path.exists())
    def test_os_lease_excludes_second_process_and_normal_exit_releases(self):
        path=config.DATA_DIR/'process-fixture.lock'
        code=('import sys\nfrom moneymin.operation_lease import operation_lease\n'
              'with operation_lease(sys.argv[1]):\n print("owned",flush=True)\n sys.stdin.readline()\n')
        child=subprocess.Popen([sys.executable,'-B','-c',code,str(path)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE,text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(),'owned')
            with self.assertRaises(OperationLeaseError):
                with operation_lease(path):pass
        finally:
            child.communicate('\n',timeout=5)
        self.assertEqual(child.returncode,0)
        with operation_lease(path):pass
        self.assertTrue(path.exists())
    def test_terminal_uuid_history_releases_review_without_fabricating_delivery(self):
        from moneymin.campaign_types import CampaignLog
        f=self.f;review=f.review();self.assertEqual(f.post(START,f.start_body(review)).status_code,200)
        log=CampaignLog('fixture-utc',[f.body['account_email']],status='stopped',start_request_id=f.body['start_request_id'])
        path=log.save()
        f.client=server.create_app(for_testing=True).test_client()
        response=f.client.get('/api/campaigns/starts/'+f.body['start_request_id'],headers=f.headers).get_json()
        self.assertTrue(response['terminal']);self.assertEqual(response['log_name'],path.name)
        self.assertEqual(response['execution_state'],'stopped');self.assertFalse(response['delivery_confirmed'])
        f.runner.start.assert_called_once()
    def test_real_offline_original_chain_resolves_uuid_and_current_receipts(self):
        # Reuse the real API→runner→engine→upload_session→transport fixture;
        # its provider/auth/media observations are explicitly inert.
        self.f.test_api_real_runner_core_multichunk_protocol_counts_one_complete_group()
        self.f.client=server.create_app(for_testing=True).test_client()
        reply=self.f.client.get('/api/campaigns/starts/'+self.f.body['start_request_id'],headers=self.f.headers)
        self.assertEqual(reply.status_code,200,reply.get_json());data=reply.get_json()
        self.assertTrue(data['terminal']);self.assertEqual(data['execution_state'],'done')
        self.assertTrue(data['delivery_confirmed']);self.assertTrue(data['log_name'].startswith('campaign_'))
        self.assertNotIn(str(self.f.root),json.dumps(data))
    def test_legacy_conflicting_or_corrupt_histories_do_not_resolve_uuid(self):
        from moneymin.campaign_types import CampaignLog
        f=self.f;review=f.review();self.assertEqual(f.post(START,f.start_body(review)).status_code,200)
        url='/api/campaigns/starts/'+f.body['start_request_id']
        log=CampaignLog('fixture-utc',[f.body['account_email']],status='done');path=log.save()
        def unresolved():self.assertFalse(f.client.get(url,headers=f.headers).get_json()['terminal'])
        unresolved() # Missing historical UUID is not inferred from timing.
        log.start_request_id=f.body['start_request_id'];log.accounts=['foreign@example.test'];log.save();unresolved()
        log.accounts=[f.body['account_email']];log.save()
        second=CampaignLog('fixture-utc',[f.body['account_email']],status='done',start_request_id=f.body['start_request_id']).save()
        unresolved();second.unlink();path.write_bytes(b'{"corrupt":');unresolved()

class DatasetStartDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.f=dataset_fixture.CampaignEndToEndTests('test_reviewed_candidates_survive_catalog_change_without_new_selection')
        self.f.setUp();self.addCleanup(self.f.doCleanups)
    def test_reviewed_dataset_uuid_survives_app_restart_without_new_runner_call(self):
        from unittest.mock import Mock
        f=self.f
        with patch.object(server,'RUNNER',Mock(running=False,total_sends=2,state='idle')) as runner:
            body={**f.body,'include_clip_plan':True}
            reviewed=f.client.post('/api/campaigns/preflight',json=body)
            self.assertEqual(reviewed.status_code,200,reviewed.get_json())
            start={**body,'preflight_id':reviewed.get_json()['preflight_id']}
            self.assertEqual(f.client.post('/api/campaigns',json=start).status_code,200)
            other=server.create_app(for_testing=True).test_client()
            reply=other.post('/api/campaigns',json=start)
            self.assertEqual(reply.status_code,200,reply.get_json());self.assertTrue(reply.get_json()['already_running'])
            self.assertEqual(reply.get_json()['start_request_id'],start['preflight_id'])
            runner.start.assert_called_once()
            self.assertEqual(other.post('/api/campaigns',json={**start,'count':2}).status_code,409)
            runner.start.assert_called_once()
    def test_uncertain_dataset_uuid_blocks_new_start_after_restart(self):
        from unittest.mock import Mock
        f=self.f
        with patch.object(server,'RUNNER',Mock(running=False,total_sends=2,state='idle')) as runner:
            body={**f.body,'include_clip_plan':True}
            reviewed=f.client.post('/api/campaigns/preflight',json=body)
            self.assertEqual(reviewed.status_code,200,reviewed.get_json())
            start={**body,'preflight_id':reviewed.get_json()['preflight_id']}
            runner.start.side_effect=RuntimeError('inert lost result after possible admission')
            self.assertEqual(f.client.post('/api/campaigns',json=start).status_code,500)
            other=server.create_app(for_testing=True).test_client()
            reply=other.post('/api/campaigns',json=start)
            self.assertEqual(reply.status_code,500,reply.get_json())
            self.assertEqual(reply.get_json()['error_code'],'start_outcome_unknown');runner.start.assert_called_once()

if __name__=='__main__':unittest.main()
