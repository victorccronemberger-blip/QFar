"""Whole-session quality and cleanup journal, with inert HTTP/media only."""
from datetime import datetime,timezone
from pathlib import Path
import json
import unittest
from unittest.mock import patch
from moneymin import upload
import test_upload_response_contract as fixtures


class GroupSession:
    email='quality-fixture@example.invalid'
    def __init__(self,statuses,cleanup=204,complete_fault=None):
        self.statuses=statuses; self.cleanup=cleanup; self.calls=[]; self.created=0
        self.complete_fault=complete_fault
    def request(self,method,path,body=None):
        self.calls.append((method,path,body))
        if path.startswith('/api/v1/uploads?'):
            self.created+=1
            return 201,json.dumps({'id':f'quality-upload-{self.created}','status':'initiated','meta':{}})
        if path=='/api/v1/storage/sas/blobs':
            return 200,json.dumps({'signed_urls':[{'filename':f['filename'],
                'blob_url':'https://blob.invalid/'+f['filename'],'expires_at':'2030-01-01T00:00:00Z'} for f in body['files']]})
        if path.endswith('/complete'):
            if self.complete_fault is not None and path.split('/')[-2]=='quality-upload-2':
                return self.complete_fault
            return 200,json.dumps({'id':path.split('/')[-2],'status':'uploaded','meta':{}})
        if path.endswith('/evaluate'):
            uid=path.split('/')[-2]; status=self.statuses[int(uid.rsplit('-',1)[1])-1]
            if status=='outage': return 503,'{}'
            check={'id':'fixture-quality','label':'Declared inert quality','status':status,'detail':None}
            if status=='incomplete': check={'id':'fixture-quality','label':'Declared inert quality','status':'pass'}
            return 200,json.dumps({'upload_id':uid,'checks':[check]})
        if method=='DELETE': return self.cleanup,''
        if path.endswith('/fail'): return (200,'{}') if self.cleanup==204 else (503,'{}')
        if path.endswith('/finalize'): return 204,''
        raise AssertionError('Unexpected inert group operation')


class PerfectGroupContractTests(unittest.TestCase):
    def setUp(self):
        f=fixtures.UploadResponseContractTests('test_new_upload_defaults_register_before_sas_and_transport')
        f.setUp(); self.addCleanup(f.doCleanups); self.fixture=f
        self.journals=f.video.parent/'journals'
        f.stack.enter_context(patch.object(upload,'sidecars_dir',return_value=self.journals))
        f.stack.enter_context(patch.object(upload.time,'time',return_value=datetime(2026,10,4,8,tzinfo=timezone.utc).timestamp()))
        self.other=f.video.parent/'other.mp4'; self.other.write_bytes(b'Declared inert second video fixture')
        self.sequence=0
    def send(self,statuses,*,evaluate=False,cleanup=204,progress=None,complete_fault=None):
        self.sequence+=1; sid=f'quality-group-{self.sequence}'
        s=GroupSession(statuses,cleanup,complete_fault)
        r=upload.upload_session(s,[self.fixture.video,self.other],'fixture-org',session_id=sid,
            task_id='fixture-task',recorded_at='2026-10-04T07:55:00.000Z',
            normalize=False,sidecar=False,finalize=True,evaluate=evaluate,require_perfect=True,
            persist_sidecar=True,max_retries=1,device_meta={},platform_meta={},video_meta={},network_meta={},on_progress=progress)
        return s,r,sid
    def test_evaluate_and_perfect_only_use_one_evaluation_per_chunk(self):
        for evaluate in (False,True):
            with self.subTest(evaluate=evaluate):
                s,r,sid=self.send(['pass','skip'],evaluate=evaluate)
                self.assertIs(r.finalized,True)
                self.assertEqual(sum(p.endswith('/evaluate') for _,p,_ in s.calls),2)
                self.assertFalse(any(m=='DELETE' or p.endswith('/fail') for m,p,_ in s.calls))
                self.assertTrue(all(upload.load_sidecar(sid,i)['evaluation_verified'] is True for i in (0,1)))
    def test_rejection_is_durable_and_cleanup_ack_is_preserved_even_when_cleanup_fails(self):
        for statuses in (['pass','fail'],['fail','pass'],['fail','fail']):
            for cleanup in (204,503):
                with self.subTest(statuses=statuses,cleanup=cleanup):
                    s,r,sid=self.send(statuses,cleanup=cleanup)
                    self.assertIs(r.finalized,False)
                    rows=[upload.load_sidecar(sid,i) for i in (0,1)]
                    for row in rows:
                        self.assertEqual(row['state'],upload.STATE_QUARANTINE)
                        self.assertEqual(row['phase'],'quality_rejected')
                        self.assertIs(row['finalized'],False)
                        self.assertIs(row['session_delete_confirmed'],cleanup==204)
                        self.assertEqual(row['session_delete_status'],cleanup)
                    for i,status in enumerate(statuses):
                        self.assertIs(rows[i].get('upload_delete_confirmed',False),status=='fail' and cleanup==204)
                        if status=='fail':
                            self.assertEqual(rows[i]['delete_upload_status'],cleanup)
                            self.assertEqual(rows[i]['fail_status'],200 if cleanup==204 else cleanup)
                    self.assertEqual(sum(m=='DELETE' and '/sessions/' in p for m,p,_ in s.calls),1)
                    before=[upload._sidecar_path(sid,i).read_bytes() for i in (0,1)]
                    s.calls.clear(); self.assertEqual(upload.pump_pending(s),[])
                    self.assertEqual(s.calls,[])
                    self.assertEqual([upload._sidecar_path(sid,i).read_bytes() for i in (0,1)],before)
    def test_uncertainty_preserves_entire_group_before_any_perfect_compensation(self):
        for unknown in ('incomplete','outage'):
            for statuses in ([unknown,'fail'],['fail',unknown]):
                with self.subTest(statuses=statuses):
                    s,r,sid=self.send(statuses)
                    self.assertIs(r.finalized,False)
                    self.assertFalse(any(m=='DELETE' or p.endswith('/fail') for m,p,_ in s.calls))
                    self.assertEqual(sum(p.endswith('/evaluate') for _,p,_ in s.calls),2)
                    for i in (0,1):
                        row=upload.load_sidecar(sid,i)
                        self.assertEqual(row['state'],upload.STATE_QUARANTINE)
                        self.assertEqual(row['phase'],'evaluation_review')
                        self.assertEqual(row['upload_id'],f'quality-upload-{i+1}')
                    s.calls.clear(); self.assertEqual(upload.pump_pending(s),[]); self.assertEqual(s.calls,[])
    def test_perfect_compensation_has_progress_gates_before_each_write(self):
        events=[]
        def progress(phase,*_args,**_kw): events.append(phase)
        s,r,_sid=self.send(['fail','fail'],progress=progress)
        self.assertIs(r.finalized,False)
        self.assertEqual(events.count('fail'),2)
        self.assertEqual(events.count('delete-upload'),2)
        self.assertEqual(events.count('delete-session'),1)

    def test_uncertain_complete_preserves_group_before_cleanup_and_keeps_origin_phase(self):
        for fault,phase in (((200,'{}'),'completion_review'),((503,'{}'),'complete')):
            with self.subTest(fault=fault):
                s,r,sid=self.send(['fail','pass'],complete_fault=fault)
                self.assertIs(r.finalized,False)
                self.assertFalse(any(m=='DELETE' or p.endswith('/fail') for m,p,_ in s.calls))
                self.assertEqual(sum(p.endswith('/evaluate') for _,p,_ in s.calls),1)
                row=upload.load_sidecar(sid,1)
                self.assertEqual(row['state'],upload.STATE_QUARANTINE)
                self.assertEqual(row['phase'],'evaluation_review')
                self.assertEqual(row['quality_review_origin_phase'],phase)
                self.assertEqual(row['upload_id'],'quality-upload-2')
                s.calls.clear(); self.assertEqual(upload.pump_pending(s),[]); self.assertEqual(s.calls,[])
