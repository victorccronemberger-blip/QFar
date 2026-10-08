"""Real ZIP recovery transport/control flow; declared inert artifacts and HTTP."""
from datetime import datetime,timezone
import hashlib,json,threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from moneymin import campaign,transport,upload
from moneymin.web.runner import CampaignRunner
import test_upload_response_contract as fixtures
from test_upload_sas_native_schema import SasSession
import test_campaign_reconciliation as caller_fixtures

REAL_PUT_BLOB=upload._put_blob


class ZipRecoveryPauseTests(unittest.TestCase):
    def execute(self,control):
        f=fixtures.UploadResponseContractTests('test_new_upload_defaults_register_before_sas_and_transport')
        f.setUp(); self.addCleanup(f.doCleanups)
        runner=CampaignRunner(); runner.state='running'
        reached,blocked,finished=threading.Event(),threading.Event(),threading.Event()
        wire,phases,results,errors=[],[],[],[]
        payload=b'Declared original ZIP bytes for recovery, not sensor observations.'
        sid='zip-pause-original'; journals=f.video.parent/'journals'
        f.stack.enter_context(patch.object(upload,'sidecars_dir',return_value=journals))
        f.stack.enter_context(patch.object(upload.time,'time',return_value=datetime(2026,10,4,8,tzinfo=timezone.utc).timestamp()))
        archive=upload._sidecar_archive_path(sid,0); archive.parent.mkdir(); archive.write_bytes(payload)
        row={'session_id':sid,'chunk_index':0,'expected_chunk_count':1,'account_email':SasSession.email,
             'org_key':'fixture-org','task_id':'fixture-task','log_id':sid+'_0','filename':sid+'_0.mp4',
             'upload_id':'fixture-upload','state':upload.STATE_RETRY_LATE,'phase':'transport',
             'recorded_at':'2026-10-04T07:55:00.000Z','local_video_path':str(f.video),'size_bytes':21,'duration_ms':60000,
             'register_first':True,'native_response_schema':True,'create_attempted':True,
             'suppress_per_chunk_catbear':True,
             'transport_artifact':'sidecar','conflict_action':'complete','sidecar_data_path':str(archive.resolve()),
             'sidecar_size_bytes':len(payload),'sidecar_sha256':hashlib.sha256(payload).hexdigest(),
             'finalize_requested':True,'finalized':False,'evaluation_required':True,'evaluation_verified':False}
        upload.save_sidecar(row); f.video.unlink()
        class Session(SasSession):
            def me(self):
                return {'email': self.email, 'resourceKey': 'fixture-user-resource'}
            def request(self,method,path,body=None):
                if method=='GET' and path=='/api/v1/uploads/fixture-upload':
                    return 200,json.dumps({'uploadId':row['upload_id'],'sessionId':row['session_id'],
                        'logId':row['log_id'],'status':'initiated','durationMs':row['duration_ms'],
                        'recordedAt':row['recorded_at'],'createdAt':row['recorded_at'],
                        'userEmail':self.email,'userResourceKey':'fixture-user-resource',
                        'orgName':'Fixture org','orgResourceKey':row['org_key'],
                        'storageAccount':'fixture-storage','taskId':row['task_id'],
                        'taskName':'Fixture task','meta':{}})
                if path.endswith('/evaluate'):
                    self.calls.append((method,path,body)); self.events.append('evaluate')
                    return 200,json.dumps({'upload_id':'fixture-upload','checks':[{'id':'inert','label':'Declared inert quality','status':'pass','detail':None}]})
                if path.endswith('/finalize'):
                    self.calls.append((method,path,body)); self.events.append('finalize'); return 204,''
                return super().request(method,path,body)
        session=Session()
        class Blob:
            def put(self,url,**kwargs):
                phase='blocklist' if 'comp=blocklist' in url else 'block'
                wire.append((phase,runner.pause_requested,kwargs['data']))
                if len(wire)==1:
                    if control=='pause': runner.pause()
                    elif control=='stop': runner.stop()
                    reached.set()
                return SimpleNamespace(status_code=201,text='')
        def progress(phase,*_args,**_kw):
            phases.append(phase)
            if runner.pause_requested: blocked.set()
            runner._checkpoint()
        for context in (patch.object(upload,'_put_blob',new=REAL_PUT_BLOB),
                        patch.object(transport,'_kind','curl'),patch.object(transport,'_cffi',Blob()),
                        patch.object(transport,'_impersonate',None),patch.object(transport,'BLOCK_SIZE',8)):
            f.stack.enter_context(context)
        def work():
            try: results.extend(upload.pump_pending(session,max_retries=1,retry_backoff=0,fail_on_error=False,on_progress=progress))
            except BaseException as exc: errors.append(exc)
            finally: finished.set()
        worker=threading.Thread(target=work,daemon=True); worker.start()
        try:
            self.assertTrue(reached.wait(2))
            if control=='pause':
                self.assertTrue(blocked.wait(.3)); self.assertFalse(finished.is_set())
                self.assertEqual(len(wire),1)
            else: self.assertTrue(finished.wait(2))
        finally:
            if runner.pause_requested: runner.resume()
            worker.join(2)
        self.assertFalse(worker.is_alive()); self.assertEqual(errors,[])
        self.assertFalse(any(paused for _,paused,_ in wire))
        self.assertEqual(b''.join(data for phase,_paused,data in wire if phase=='block'),payload)
        self.assertEqual(sum(p=='blocklist' for p,_,_ in wire),1)
        self.assertEqual(session.events,['sas','complete','evaluate','finalize'])
        f.video_put.assert_not_called()
        self.assertEqual(results[0]['upload_id'],'fixture-upload')
        self.assertEqual(results[0]['recorded_at'],row['recorded_at']); self.assertIs(results[0]['finalized'],True)
        self.assertTrue(all(phase in phases for phase in ('sas','transport','complete','evaluate','finalize')))
    def test_original_zip_recovery_waits_between_blocks_until_resume(self): self.execute('pause')
    def test_stop_drains_original_zip_without_new_create_or_video(self): self.execute('stop')
    def test_unpaused_original_zip_keeps_bytes_and_receipt(self): self.execute('none')
    def test_campaign_forwards_its_progress_callback_to_pending_recovery(self):
        f=caller_fixtures.CampaignReconciliationTests('test_recovered_session_replaces_new_upload_and_updates_registry')
        f.setUp(); self.addCleanup(f.doCleanups)
        session=Mock(_live=True,recording_policy=None); session._moneymin_pending_pumped=False
        callback=Mock()
        r=campaign.upload_to_account(f.item,f.account,'task',30,True,True,session=session,on_progress=callback)
        self.assertIs(r['ok'],True)
        self.assertIs(campaign.pump_pending.call_args.kwargs.get('on_progress'),callback)
