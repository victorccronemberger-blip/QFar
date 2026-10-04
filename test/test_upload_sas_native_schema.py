"""Native SAS string fields, using declared inert media/provider fixtures."""
import json
import unittest
from unittest.mock import patch
from moneymin import upload
import test_upload_response_contract as fixtures


class SasSession(fixtures.FakeSession):
    email = 'sas-fixture@example.invalid'
    def __init__(self, transform=None, *, uploaded=False):
        super().__init__(create_status=409 if uploaded else 201,
            create_body={'upload_id':'fixture-upload','status':'uploaded'} if uploaded else None)
        self.transform=transform
        self.sas_count=0
    def request(self,method,path,body=None):
        if path=='/api/v1/storage/sas/blobs':
            self.calls.append((method,path,body)); self.events.append('sas'); self.sas_count+=1
            entries=[{'filename':row['filename'],'blob_url':'https://blob.invalid/'+row['filename']+'?sig=PRIVATE_SAS_FIXTURE',
                      'expires_at':'2030-01-01T00:00:00Z'} for row in body['files']]
            if self.transform: entries=[self.transform(e,self.sas_count) for e in entries]
            return 200,json.dumps({'signed_urls':entries})
        return super().request(method,path,body)


class NativeSasSchemaTests(unittest.TestCase):
    def setUp(self):
        f=fixtures.UploadResponseContractTests('test_new_upload_defaults_register_before_sas_and_transport')
        f.setUp(); self.addCleanup(f.doCleanups); self.fixture=f
    def test_expiry_type_is_validated_for_all_requested_artifacts_before_any_put(self):
        values=('missing',None,True,1,1.2,[],{})
        for mode in ('mp4','both','uploaded-zip'):
            for value in values:
                with self.subTest(mode=mode,value=value):
                    def transform(entry,_count):
                        # In the two-artifact case, only the ZIP is malformed;
                        # MP4 must not begin before validation of the whole list.
                        if mode=='both' and entry['filename'].endswith('.mp4'): return entry
                        entry=dict(entry)
                        if value=='missing': entry.pop('expires_at')
                        else: entry['expires_at']=value
                        return entry
                    s=SasSession(transform,uploaded=mode=='uploaded-zip')
                    r=self.fixture.run_chunk(s,sidecar=mode!='mp4')
                    self.assertEqual(r.state,upload.STATE_FAILED)
                    self.assertEqual(r.upload_id,'fixture-upload')
                    self.assertEqual(self.fixture.checkpoint.call_args.kwargs['phase'],'sas')
                    self.fixture.video_put.assert_not_called(); self.fixture.zip_put.assert_not_called()
                    self.assertEqual(s.events,['create','sas'])
                    self.assertNotIn('PRIVATE_SAS_FIXTURE',r.error)
    def test_native_schema_accepts_strings_without_inventing_date_semantics(self):
        for text in ('','declared-inert-not-a-date','2000-01-01T00:00:00Z','2030-01-01T00:00:00Z'):
            with self.subTest(text=text):
                s=SasSession(lambda e,_c:{**e,'expires_at':text})
                self.assertEqual(self.fixture.run_chunk(s,sidecar=True).state,upload.STATE_DONE)
    def test_legacy_order_preserves_sparse_sas_response_compatibility(self):
        s=SasSession(lambda e,_c:{k:v for k,v in e.items() if k!='expires_at'})
        r=self.fixture.run_chunk(s,register_first=False,sidecar=True)
        self.assertEqual(r.state,upload.STATE_DONE)
        self.assertEqual(s.events,['sas','put-video','put-sidecar','create','complete'])
    def test_malformed_remint_does_not_retry_a_stale_zip_authorization(self):
        s=SasSession(lambda e,c:e if c==1 else {k:v for k,v in e.items() if k!='expires_at'})
        count=0
        def put(_url,_data,**_kw):
            nonlocal count
            count+=1
            if count==1: raise upload.UploadError('inert expired SAS',status_code=403,transient=False)
            return 201
        with patch.object(upload,'_put_blob',side_effect=put):
            r=self.fixture.run_chunk(s,sidecar=True)
        self.assertEqual(r.state,upload.STATE_FAILED)
        self.assertEqual(r.upload_id,'fixture-upload')
        self.assertEqual(count,1)
        self.assertEqual(s.events,['create','sas','put-video','sas'])
        self.assertEqual(s.sas_count,2)
