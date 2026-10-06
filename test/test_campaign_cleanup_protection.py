"""Actual journals and cache markers; all media/identities are inert fixtures."""
from pathlib import Path
from contextlib import ExitStack
import hashlib,json,tempfile,unittest
import threading
from unittest.mock import patch

from moneymin import campaign,config,upload,recovery,upload_storage

class CleanupProtectionTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.root=Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='qmoney-cleanup-fixture-')))
        self.stack.enter_context(patch.multiple(config,DATA_DIR=self.root/'state',MEDIA_DATA_DIR=self.root/'media',SECRETS_DIR=self.root/'secrets'))
        self.work=self.root/'media'/'ego4d';self.work.mkdir(parents=True)
        self.stack.enter_context(patch.object(campaign.holoassist,'data_dir',return_value=self.root/'holo'))
        self.assertTrue(upload.sidecars_dir().resolve().is_relative_to(self.root.resolve()))
    def media(self,name):
        path=self.work/name;path.write_bytes(b'declared inert media')
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        path.with_name(path.name+'.source.json').write_text(json.dumps({'version':campaign._NATIVE_CACHE_VERSION,
            'source_sha256':digest,'prepared_size':path.stat().st_size,'prepared_sha256':digest}),encoding='utf8')
        return path
    def row(self,video,index=0,count=1,**extra):
        return dict(session_id='pending-fixture',account_email='owner@example.test',org_key='org-fixture',task_id='task-fixture',
            chunk_index=index,expected_chunk_count=count,state='failed',phase='create',create_attempted=True,
            video_path=str(video),campaign_context={'registry_key':'minute|task-fixture','clip_uid':'clip-fixture'},**extra)
    def test_healthy_owned_marker_is_cleaned_and_foreign_is_preserved(self):
        owned=self.media('owned.mp4');foreign=self.work/'foreign.mp4';foreign.write_bytes(b'foreign fixture')
        result=campaign.cleanup_media_cache(self.work,provider='ego4d')
        self.assertFalse(owned.exists());self.assertFalse(owned.with_name(owned.name+'.source.json').exists())
        self.assertEqual(result['files'],2);self.assertEqual(foreign.read_bytes(),b'foreign fixture')
    def test_actual_pending_two_part_journals_protect_each_media_and_marker(self):
        paths=[self.media('part0.mp4'),self.media('part1.mp4')]
        for i,path in enumerate(paths):upload.save_sidecar(self.row(path,i,2))
        before={p:p.read_bytes() for p in [*paths,*(p.with_name(p.name+'.source.json') for p in paths),*upload.sidecars_dir().glob('*.json')]}
        self.assertEqual(campaign.cleanup_media_cache(self.work)['files'],0)
        self.assertEqual({p:p.read_bytes() for p in before},before)
    def test_corrupt_journal_prevents_any_cleanup_and_preserves_bytes(self):
        owned=self.media('owned.mp4');bad=upload.sidecars_dir()/'corrupt.json';raw=b'{"session_id":"x","session_id":"y"}';bad.write_bytes(raw)
        with self.assertRaises(ValueError):campaign.cleanup_media_cache(self.work)
        self.assertTrue(owned.exists());self.assertEqual(bad.read_bytes(),raw)
    def test_legacy_unknown_reservation_schema_and_corrupt_ledger_fail_closed(self):
        owned=self.media('owned.mp4')
        for filename in ('original_capture_reservations.json','start_requests.json'):
            path=config.DATA_DIR/filename;path.write_bytes(b'[]')
            with self.assertRaises(ValueError):campaign.cleanup_media_cache(self.work)
            self.assertTrue(owned.exists());self.assertEqual(path.read_bytes(),b'[]');path.unlink()
    def test_owner_id_count_and_create_intent_cannot_be_downgraded(self):
        path=self.media('receipt.mp4');row=self.row(path,upload_id='known-upload')
        journal=upload.save_sidecar(row);before=journal.read_bytes()
        for changes in ({'account_email':'other@example.test'},{'org_key':'other-org'},{'task_id':'other-task'},
                        {'expected_chunk_count':2},{'upload_id':''},{'create_attempted':False}):
            with self.subTest(changes=tuple(changes)):
                with self.assertRaises(upload.UploadError):upload.save_sidecar({**row,**changes})
                self.assertEqual(journal.read_bytes(),before)
    def test_media_publication_and_cleanup_share_an_actual_exclusion_barrier(self):
        from moneymin.media_lifecycle import media_state_lease
        owned=self.media('owned.mp4');held,release=threading.Event(),threading.Event()
        def hold():
            with media_state_lease():
                held.set();release.wait(5)
        worker=threading.Thread(target=hold);worker.start()
        try:
            self.assertTrue(held.wait(2))
            # Keep the actual barrier held while only the publisher's clock
            # passes its bounded 30-second deadline. Cleanup remains immediate.
            with patch('moneymin.media_lifecycle.time.monotonic', side_effect=[0.0, 30.1]):
                with self.assertRaises(upload.UploadError):upload.save_sidecar(self.row(owned))
            with self.assertRaises(ValueError):campaign.cleanup_media_cache(self.work)
            self.assertFalse(upload._sidecar_path('pending-fixture',0).exists())
            self.assertTrue(owned.exists())
        finally:
            release.set();worker.join(2)
        self.assertFalse(worker.is_alive())
        upload.save_sidecar(self.row(owned))
        self.assertEqual(campaign.cleanup_media_cache(self.work)['files'],0);self.assertTrue(owned.exists())

if __name__=='__main__':unittest.main()
