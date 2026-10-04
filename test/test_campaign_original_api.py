"""Original API admission: real local ZIP/CSV/group checks; inert auth/runner.

The fixture MP4 and sensor CSVs are declared generated laboratory inputs, not
physical capture. No external provider request or real account is exercised.
"""
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch
import hashlib
import importlib.util
import json
import socket
import tempfile
import unittest

from moneymin import config, minute_api, sidecar
from moneymin.service_policy import RecordingPolicy
from moneymin.web import server
from test_original_capture_workflow import fixture_pair, fixture_probe, EPOCH, OWNER, ORG, TASK

PREFLIGHT = '/api/campaigns/original/preflight'
START = '/api/campaigns/original'
CATALOG = '/api/campaigns/original/tasks'


class OriginalCampaignApiTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='qmoney-original-api-fixture-')))
        self.stack.enter_context(patch.multiple(config, DATA_DIR=self.root/'data', MEDIA_DATA_DIR=self.root/'library', SECRETS_DIR=self.root/'secrets'))
        self.stack.enter_context(patch.dict(server.os.environ, {'QMONEY_LOCAL_API_TOKEN': 'inert-original-api-token'}))
        self.stack.enter_context(patch.object(socket.socket, 'connect', side_effect=AssertionError('external network forbidden')))
        self.stack.enter_context(patch.object(minute_api, '_request', side_effect=AssertionError('provider forbidden')))
        self.stack.enter_context(patch.object(server.time, 'time', return_value=EPOCH))
        self.stack.enter_context(patch.object(sidecar, 'probe_video', side_effect=fixture_probe))
        self.runner = Mock(running=False, state='idle', total_sends=1)
        self.stack.enter_context(patch.object(server, 'RUNNER', self.runner))
        self.stack.enter_context(patch.object(server, 'HOLO_CACHE_RUNNER', Mock(running=False)))
        self.stack.enter_context(patch.object(server, 'RECOVERY', Mock(running=False)))
        self.stack.enter_context(patch.object(server.recovery, 'snapshot', return_value={'items': []}))
        self.stack.enter_context(patch.object(server, '_list_accounts', return_value=[{'email': OWNER}]))
        self.resolve = self.stack.enter_context(patch.object(server, '_resolve_org', return_value=ORG))
        self.fingerprint = self.stack.enter_context(patch.object(server, '_preflight_fingerprint', return_value='owner-context-v1'))
        self.session = Mock(email=OWNER, recording_policy=RecordingPolicy(1, 60_000, 1_800_000, 14_400_000), device_camera_allowed=True)
        self.session.all_tasks.return_value = [{'id': TASK, 'name': 'Declared fixture task', 'description': 'No dataset is consulted.'}]
        self.session._check_write_policy.return_value = None
        self.auth = self.stack.enter_context(patch.object(server.Session, 'from_email', return_value=self.session))
        self.dataset = self.stack.enter_context(patch.object(server.campaign, 'available_tasks', side_effect=AssertionError('dataset catalog forbidden')))
        self.client = server.create_app(for_testing=True).test_client()
        self.headers = {'X-QMoney-Session': 'inert-original-api-token'}
        self.pairs = [fixture_pair(self.root/'source', index, count=2) for index in range(2)]
        self.body = {'account_email': OWNER, 'task_id': TASK, 'file_pairs': [{'video_path': str(a), 'sidecar_path': str(b)} for a,b in self.pairs],
                     'expected_chunk_count': 2, 'evaluate': True, 'finalize': True,
                     'start_request_id': '12345678-1234-4234-8234-123456789abc'}
        self.originals = {p:p.read_bytes() for pair in self.pairs for p in pair}

    def post(self, route, body=None):
        return self.client.post(route, json=self.body if body is None else body, headers=self.headers)

    def review(self):
        response=self.post(PREFLIGHT)
        self.assertEqual(response.status_code, 200, response.get_json())
        body=response.get_json()
        self.assertIsInstance(body, dict)
        self.assertTrue(body['ok'])
        self.assertIsInstance(body['preflight_id'], str)
        self.assertTrue(body['preflight_id'])
        self.assertEqual(body['receipt'], body['preflight_id'])
        return body

    def start_body(self, review):
        return {**self.body, 'preflight_id': review['preflight_id']}

    def assert_preserved(self):
        self.assertEqual({p:p.read_bytes() for p in self.originals}, self.originals)

    def test_missing_feature_reports_as_normal_contract_failure_not_collection_error(self):
        self.review()
        self.assert_preserved()

    def test_preflight_is_source_bound_private_and_never_starts_runner(self):
        response=self.review(); summary=response['original_summary']
        self.assertEqual(summary['group_count'], 1)
        self.assertEqual(summary['chunks'], 2)
        self.assertEqual(summary['expected_chunk_count'], 2)
        self.assertEqual(summary['total_duration_ms'], 120_000)
        self.assertEqual(summary['task_binding']['task_id'], TASK)
        self.assertEqual(summary['task_binding']['name'], 'Declared fixture task')
        self.assertFalse(summary['physical_provenance_verified'])
        self.assertFalse(summary['provider_acceptance_verified'])
        self.assertTrue(response['readiness']['ready'])
        self.assertEqual(response['completion_policy'], 'explicit_finalize')
        for key in ('plan_digest','content_digest'):
            self.assertRegex(summary[key], r'^[0-9a-f]{64}$')
        self.assertNotIn(str(self.root), json.dumps(response))
        self.assertNotIn('timebase', json.dumps(response))
        self.runner.start.assert_not_called()
        self.dataset.assert_not_called()
        self.assert_preserved()

    def test_local_bad_files_and_group_fail_before_auth(self):
        for pairs in ([], [{'video_path':str(self.root/'private-canary'), 'sidecar_path':str(self.pairs[0][1])}],
                      [self.body['file_pairs'][1], self.body['file_pairs'][0]],
                      [self.body['file_pairs'][0], self.body['file_pairs'][0]]):
            with self.subTest(pairs=len(pairs)):
                self.auth.reset_mock()
                response=self.post(PREFLIGHT, {**self.body,'file_pairs':pairs})
                self.assertEqual(response.status_code,400,response.get_json())
                self.auth.assert_not_called()
                self.assertNotIn(str(self.root), json.dumps(response.get_json()))
                self.runner.start.assert_not_called()
        self.assert_preserved()

    def test_boolean_or_nonpositive_count_and_disabled_policies_fail_before_auth(self):
        for changes in ({'expected_chunk_count':True},{'expected_chunk_count':0},{'expected_chunk_count':1},
                        {'evaluate':False},{'finalize':False},{'evaluate':'true'},{'start_request_id':''},
                        {'dataset':'all'},{'plan':{'private':'inert-untrusted'}}):
            with self.subTest(changes=tuple(changes)):
                self.auth.reset_mock()
                response=self.post(PREFLIGHT,{**self.body,**changes})
                self.assertEqual(response.status_code,400,response.get_json())
                self.auth.assert_not_called()
                self.runner.start.assert_not_called()

    def test_local_wrong_owner_task_and_malformed_csv_fail_before_auth(self):
        for changes in (lambda m:m.update(account_email='other@example.test'),lambda m:m.update(task_id='other-task')):
            a,b=fixture_pair(self.root/'invalid',change=changes,count=1)
            self.auth.reset_mock()
            response=self.post(PREFLIGHT,{**self.body,'file_pairs':[{'video_path':str(a),'sidecar_path':str(b)}],'expected_chunk_count':1})
            self.assertEqual(response.status_code,400,response.get_json())
            self.auth.assert_not_called()
        import zipfile
        a,b=fixture_pair(self.root/'bad-csv',count=1)
        with zipfile.ZipFile(b) as archive:
            members=[(entry.filename,archive.read(entry)) for entry in archive.infolist()]
        with zipfile.ZipFile(b,'w') as archive:
            for name,payload in members:
                archive.writestr(name,b'not native csv' if name.endswith('.imu.csv') else payload)
        self.auth.reset_mock()
        response=self.post(PREFLIGHT,{**self.body,'file_pairs':[{'video_path':str(a),'sidecar_path':str(b)}],'expected_chunk_count':1})
        self.assertEqual(response.status_code,400,response.get_json())
        self.auth.assert_not_called()

    def test_authoritative_catalog_and_policy_rejections_remain_private(self):
        for fault in ('missing-task','camera-quota-version','owner','org'):
            with self.subTest(fault=fault):
                self.session.all_tasks.return_value=[] if fault=='missing-task' else [{'id':TASK,'name':'Task'}]
                self.session._check_write_policy.side_effect=minute_api.AuthError('private-canary-'+str(self.root),code='policy') if fault=='camera-quota-version' else None
                self.session.email='other@example.test' if fault=='owner' else OWNER
                self.resolve.return_value='other-org' if fault=='org' else ORG
                # Explicit source org belongs to ORG, rather than an invented placeholder.
                a,b=fixture_pair(self.root/'binding',count=1,change=lambda m:m.update(org_key=ORG))
                response=self.post(PREFLIGHT,{**self.body,'file_pairs':[{'video_path':str(a),'sidecar_path':str(b)}],'expected_chunk_count':1})
                self.assertEqual(response.status_code,400,response.get_json())
                self.assertNotIn('private-canary',json.dumps(response.get_json()))
                self.assertNotIn(str(self.root),json.dumps(response.get_json()))
                self.runner.start.assert_not_called()

    def test_standalone_catalog_uses_all_tasks_without_dataset_and_authenticates_local_client(self):
        response=self.client.get(CATALOG+'?account_email='+OWNER,headers=self.headers)
        self.assertEqual(response.status_code,200,response.get_json())
        self.assertEqual(response.get_json()['tasks'][0]['id'],TASK)
        self.session.all_tasks.assert_called_with(ORG)
        self.dataset.assert_not_called()
        self.auth.reset_mock()
        response=self.client.get(CATALOG+'?account_email='+OWNER)
        self.assertEqual(response.status_code,401)
        self.auth.assert_not_called()

    def test_start_requires_receipt_and_matching_frozen_body(self):
        response=self.post(START)
        self.assertEqual(response.status_code,400,response.get_json())
        self.runner.start.assert_not_called()
        reviewed=self.review()
        for changes in ({'task_id':'other-task'},{'start_request_id':'changed-request'}, {'file_pairs':list(reversed(self.body['file_pairs']))}):
            with self.subTest(changes=tuple(changes)):
                response=self.post(START,{**self.start_body(reviewed),**changes})
                self.assertEqual(response.status_code,409,response.get_json())
                self.runner.start.assert_not_called()

    def test_changed_source_and_owner_after_review_do_not_consume_receipt(self):
        reviewed=self.review();media=self.pairs[0][0];original=media.read_bytes()
        media.write_bytes(original+b'changed')
        response=self.post(START,self.start_body(reviewed))
        self.assertEqual(response.status_code,409,response.get_json())
        self.runner.start.assert_not_called()
        media.write_bytes(original)
        self.fingerprint.return_value='owner-context-v2'
        response=self.post(START,self.start_body(reviewed))
        self.assertEqual(response.status_code,409,response.get_json())
        self.runner.start.assert_not_called()
        self.fingerprint.return_value='owner-context-v1'
        response=self.post(START,self.start_body(reviewed))
        self.assertEqual(response.status_code,200,response.get_json())
        self.runner.start.assert_called_once()
        self.assert_preserved()

    def test_ttl_is_monotonic_and_rechecked_before_admission(self):
        with patch.object(server.time,'monotonic',return_value=100):reviewed=self.review()
        with patch.object(server.time,'monotonic',return_value=701):
            response=self.post(START,self.start_body(reviewed))
        self.assertEqual(response.status_code,409,response.get_json())
        self.assertEqual(response.get_json()['error_code'],'original_preflight_expired')
        self.runner.start.assert_not_called()

    def test_start_uses_exact_original_plan_and_safe_one_account_configuration(self):
        reviewed=self.review();response=self.post(START,self.start_body(reviewed))
        self.assertEqual(response.status_code,200,response.get_json())
        self.runner.start.assert_called_once()
        cfg=self.runner.start.call_args.args[0]
        self.assertEqual(cfg.start_request_id,self.body['start_request_id'])
        self.assertEqual((cfg.accounts[0].email,cfg.accounts[0].org_key,cfg.tasks[0].task_id),(OWNER,ORG,TASK))
        self.assertEqual((len(cfg.accounts),len(cfg.tasks),cfg.account_workers,cfg.tasks[0].count),(1,1,1,1))
        for flag in ('shuffle_schedule','cleanup_after_upload','unique_video','realistic_timeline','share_clips','allow_new_accounts'):
            self.assertIs(getattr(cfg,flag),False)
        self.assertIsNone(cfg.candidate_plan)
        self.assertEqual(cfg.original_capture_plan.digest,reviewed['original_summary']['plan_digest'])
        self.assertEqual(tuple(c.sidecar.sha256 for c in cfg.original_capture_plan.captures),tuple(hashlib.sha256(b.read_bytes()).hexdigest() for a,b in self.pairs))
        self.assert_preserved()

    def test_successful_start_retry_is_exactly_once_for_same_id_and_body(self):
        reviewed=self.review();body=self.start_body(reviewed)
        first=self.post(START,body);second=self.post(START,body)
        self.assertEqual(first.status_code,200,first.get_json())
        self.assertEqual(second.status_code,200,second.get_json())
        self.assertTrue(second.get_json()['already_running'])
        self.assertEqual(second.get_json()['start_request_id'],self.body['start_request_id'])
        self.runner.start.assert_called_once()
        different=self.post(START,{**body,'start_request_id':'different-request'})
        self.assertEqual(different.status_code,409,different.get_json())
        self.runner.start.assert_called_once()

    def test_busy_and_drain_do_not_start_original_plan(self):
        reviewed=self.review();self.runner.running=True
        response=self.post(START,self.start_body(reviewed))
        self.assertEqual(response.status_code,409,response.get_json())
        self.runner.start.assert_not_called()
        self.runner.running=False
        self.post('/api/campaigns/drain',{})
        response=self.post(START,self.start_body(reviewed))
        self.assertEqual(response.status_code,409,response.get_json())
        self.assertEqual(response.get_json()['error_code'],'campaign_closing')
        self.runner.start.assert_not_called()

    def test_owner_changed_while_waiting_heavy_lock_is_rejected_before_start(self):
        reviewed=self.review()
        class AdmissionLock:
            entries=0
            def __enter__(this):
                this.entries+=1
                if this.entries==2:self.fingerprint.return_value='owner-context-changed-at-admission'
            def __exit__(this,*args):return False
        with patch.object(server,'_HEAVY_RUNNER_LOCK',AdmissionLock()):
            response=self.post(START,self.start_body(reviewed))
        self.assertEqual(response.status_code,409,response.get_json())
        self.runner.start.assert_not_called()

    # Additive controls authored after the first candidate. They are not
    # attributed to the original 14-method missing-feature BEFORE witness.
    def test_ambiguous_nonfinite_and_oversized_raw_json_fail_before_auth(self):
        body=json.dumps(self.body)
        raws=['{"account_email":"other@example.test",'+body[1:], body.replace('"expected_chunk_count": 2','"expected_chunk_count": NaN'),
              '{"file_pairs":[],"file_pairs":[]}', '{"x":"'+'x'*(1024*1024)+'"}']
        for raw in raws:
            with self.subTest(raw_bytes=len(raw)):
                self.auth.reset_mock()
                response=self.client.post(PREFLIGHT,data=raw,content_type='application/json',headers=self.headers)
                self.assertEqual(response.status_code,400,response.get_json())
                self.auth.assert_not_called()
                self.runner.start.assert_not_called()

    def test_missing_or_unknown_camera_source_fails_before_auth(self):
        for cameras in (None,[],[{'source':'invented-camera'}]):
            with self.subTest(cameras=cameras):
                a,b=fixture_pair(self.root/'camera',count=1,change=lambda m:m.update(cameras=cameras))
                self.auth.reset_mock()
                response=self.post(PREFLIGHT,{**self.body,'file_pairs':[{'video_path':str(a),'sidecar_path':str(b)}],'expected_chunk_count':1})
                self.assertEqual(response.status_code,400,response.get_json())
                self.auth.assert_not_called()

    def test_extra_archive_member_is_preserved_and_is_not_malformed_required_csv(self):
        import zipfile
        with zipfile.ZipFile(self.pairs[0][1],'a') as archive:archive.writestr('unconsumed-extra.csv',b'opaque optional bytes')
        before=self.pairs[0][1].read_bytes()
        review=self.review()
        self.assertEqual(review['original_summary']['files'][0]['sidecar']['sha256'],hashlib.sha256(before).hexdigest())
        self.assertEqual(self.pairs[0][1].read_bytes(),before)

    def test_start_exception_is_private_and_uuid_cannot_be_reposted(self):
        reviewed=self.review();body=self.start_body(reviewed)
        self.runner.start.side_effect=RuntimeError('private-canary '+str(self.root))
        first=self.post(START,body);second=self.post(START,body)
        for response in (first,second):
            self.assertEqual(response.status_code,500,response.get_json())
            self.assertEqual(response.get_json()['error_code'],'original_start_outcome_unknown')
            self.assertNotIn('private-canary',json.dumps(response.get_json()))
        self.runner.start.assert_called_once()
        self.runner.start.side_effect=None
        new_review=self.review()
        conflict=self.post(START,self.start_body(new_review))
        self.assertEqual(conflict.status_code,409,conflict.get_json())
        self.assertEqual(conflict.get_json()['error_code'],'original_start_conflict')
        self.runner.start.assert_called_once()

    def test_source_org_and_catalog_are_rechecked_at_admission_without_rewriting_task(self):
        for mutation in ('media','org','catalog'):
            with self.subTest(mutation=mutation):
                reviewed=self.review();media=self.pairs[0][0];original=media.read_bytes()
                class AdmissionLock:
                    entries=0
                    def __enter__(this):
                        this.entries+=1
                        if this.entries==2:
                            if mutation=='media':media.write_bytes(original+b'changed-at-admission')
                            elif mutation=='org':self.resolve.return_value='other-org'
                            else:self.session.all_tasks.return_value=[]
                    def __exit__(this,*args):return False
                with patch.object(server,'_HEAVY_RUNNER_LOCK',AdmissionLock()):response=self.post(START,self.start_body(reviewed))
                self.assertIn(response.status_code,(400,409),response.get_json())
                self.runner.start.assert_not_called()
                media.write_bytes(original);self.resolve.return_value=ORG
                self.session.all_tasks.return_value=[{'id':TASK,'name':'Declared fixture task'}]
        reviewed=self.review();self.session.all_tasks.return_value=[{'id':TASK,'name':'Changed after review'}]
        response=self.post(START,self.start_body(reviewed))
        self.assertEqual(response.status_code,200,response.get_json())
        self.assertEqual(self.runner.start.call_args.args[0].tasks[0].task_name,'Declared fixture task')

    def test_current_write_policy_rejection_preserves_reviewed_receipt(self):
        reviewed=self.review()
        self.session._check_write_policy.side_effect=minute_api.AuthError('private quota canary',code='policy')
        response=self.post(START,self.start_body(reviewed))
        self.assertEqual(response.status_code,400,response.get_json())
        self.assertEqual(response.get_json()['error_code'],'original_policy_unavailable')
        self.runner.start.assert_not_called()
        self.session._check_write_policy.side_effect=None
        response=self.post(START,self.start_body(reviewed))
        self.assertEqual(response.status_code,200,response.get_json())
        self.runner.start.assert_called_once()
        self.assert_preserved()

    def test_api_real_runner_core_multichunk_protocol_counts_one_complete_group(self):
        """Actual API/runner/engine/uploader/transport; inert provider/probe.

        Generated sentinel MP4/native-shaped CSVs are not acquired media.
        Org/quota/camera observations and quality replies are fixture data.
        """
        from types import SimpleNamespace
        from urllib.parse import urlsplit
        from moneymin import transport, upload
        from moneymin.web.runner import CampaignRunner
        runner=CampaignRunner();api=[];blocks={};committed={}
        class Protocol:
            email=OWNER
            recording_policy=RecordingPolicy(1,60_000,1_800_000,14_400_000)
            device_camera_allowed=True
            initialization_errors={};_initialization_causes={}
            _check_write_policy=minute_api.Session._check_write_policy
            _check_local_restrictions=minute_api.Session._check_local_restrictions
            _check_recording_geo=minute_api.Session._check_recording_geo
            def warmup(this):pass
            def org_state(this,org):return {'blocked':False,'userState':'active','cameraSources':['built-in','external']}
            def recording_geo(this,org):return {'recordingAuthorization':'APPROVED','canUpload':True,'blockedReason':None}
            def ensure_auth(this,**kwargs):pass
            def all_tasks(this,org):return [{'id':TASK,'name':'Declared fixture task'}]
            def request(this,method,path,body=None):
                this._check_write_policy(method,path,body);api.append((method,path,body))
                if path.startswith('/api/v1/uploads?'):
                    return 201,json.dumps({'id':'fixture_api_receipt_'+str(body['meta']['chunk_index']),'status':'initiated','meta':{}})
                if path=='/api/v1/storage/sas/blobs':
                    return 200,json.dumps({'signed_urls':[{'filename':f['filename'],'blob_url':'https://inert.invalid/'+f['filename'],'expires_at':'inert-string'} for f in body['files']]})
                if path.endswith('/complete'):
                    return 200,json.dumps({'id':path.split('/')[-2],'status':'uploaded','meta':{}})
                if path.endswith('/evaluate'):
                    return 200,json.dumps({'upload_id':path.split('/')[-2],'checks':[{'id':'fixture','label':'Declared inert check','detail':None,'status':'pass'}]})
                if path.endswith('/finalize'):return 204,''
                raise AssertionError('Unexpected fixture operation')
            def put(this,url,**kwargs):
                name=urlsplit(url).path.lstrip('/')
                if 'comp=blocklist' in url:committed[name]=b''.join(blocks.get(name,[]))
                else:blocks.setdefault(name,[]).append(kwargs['data'])
                return SimpleNamespace(status_code=201,text='')
        protocol=Protocol()
        with ExitStack() as stack:
            stack.enter_context(patch.object(server,'RUNNER',runner))
            stack.enter_context(patch.object(server.Session,'from_email',return_value=protocol))
            stack.enter_context(patch.object(upload,'probe_video',side_effect=fixture_probe))
            stack.enter_context(patch.object(transport,'_kind','curl'))
            stack.enter_context(patch.object(transport,'_cffi',protocol))
            stack.enter_context(patch.object(transport,'_impersonate',None))
            stack.enter_context(patch.object(transport,'BLOCK_SIZE',64))
            stack.enter_context(patch.object(upload.time,'sleep',return_value=None))
            reviewed=self.review();response=self.post(START,self.start_body(reviewed))
            self.assertEqual(response.status_code,200,response.get_json())
            self.addCleanup(runner.stop)
            runner._thread.join(5)
            self.assertFalse(runner._thread.is_alive())
            self.assertEqual(runner.state,'done',runner.error)
        self.assertEqual((runner.ok_sends,runner.failed_sends,runner.done_sends),(1,0,1))
        self.assertEqual(runner.account_seconds[OWNER],120)
        creates=[body for method,path,body in api if path.startswith('/api/v1/uploads?')]
        self.assertEqual(len(creates),2)
        for index,body in enumerate(creates):
            self.assertEqual(body['duration_ms'],60_000)
            self.assertEqual(committed[body['log_id']+'.mp4'],self.pairs[index][0].read_bytes())
            self.assertEqual(committed[body['log_id']+'.data.zip'],self.pairs[index][1].read_bytes())
        self.assertEqual(sum(path.endswith('/evaluate') for method,path,body in api),2)
        self.assertEqual(sum(path.endswith('/finalize') for method,path,body in api),1)
        self.assertEqual(next(body for method,path,body in api if path.endswith('/finalize')),{'expected_chunk_count':2})
        self.assert_preserved()

    def test_receipt_ttl_expiring_during_final_probe_never_claims_uuid_or_starts(self):
        from moneymin import original_capture
        clock={'value':100};calls={'count':0}
        with patch.object(server.time,'monotonic',side_effect=lambda:clock['value']):
            reviewed=self.review()
            verify=original_capture.verify_original_capture_plan
            def observed(*args,**kwargs):
                value=verify(*args,**kwargs);calls['count']+=1
                if calls['count']==2:clock['value']=701
                return value
            with patch.object(original_capture,'verify_original_capture_plan',side_effect=observed):
                response=self.post(START,self.start_body(reviewed))
            self.assertEqual(response.status_code,409,response.get_json())
            self.assertEqual(response.get_json()['error_code'],'original_preflight_expired')
            self.runner.start.assert_not_called()
            # A restored laboratory clock demonstrates that no UUID claim was
            # made before the definitive rejection; production time is monotonic.
            clock['value']=200
            response=self.post(START,self.start_body(reviewed))
            self.assertEqual(response.status_code,200,response.get_json())
            self.runner.start.assert_called_once()

    def test_optional_blank_catalog_label_uses_authoritative_id_and_invalid_json_root_is_private(self):
        self.session.all_tasks.return_value=[{'id':TASK,'name':'   '}]
        reviewed=self.review()
        self.assertEqual(reviewed['original_summary']['task_binding']['name'],TASK)
        for root in ([],None,1,'private-canary'):
            with self.subTest(root_type=type(root).__name__):
                self.auth.reset_mock()
                response=self.client.post(PREFLIGHT,data=json.dumps(root),content_type='application/json',headers=self.headers)
                self.assertEqual(response.status_code,400)
                self.auth.assert_not_called()
                self.assertNotIn('private-canary',response.get_data(as_text=True))


if __name__=='__main__':unittest.main()
