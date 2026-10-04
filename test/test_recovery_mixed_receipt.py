"""A valid pending chunk cannot authorize recovery past an invalid sibling."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from moneymin import config, recovery, upload


class RecoveryMixedReceiptTests(unittest.TestCase):
    def execute(self, upload_id, *, invalid=True):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            journals = root / 'journals'
            journals.mkdir()
            for context in (patch.object(config, 'DATA_DIR', root),
                            patch.object(config, 'MEDIA_DATA_DIR', root),
                            patch.object(upload, 'sidecars_dir', return_value=journals)):
                stack.enter_context(context)
            base = {'session_id': 'mixed-session', 'account_email': 'mixed@example.invalid',
                    'org_key': 'fixture-org', 'task_id': 'fixture-task', 'expected_chunk_count': 2,
                    'finalize_requested': True, 'evaluation_required': False,
                    'campaign_context': {'registry_key': 'fixture-key', 'clip_uid': 'fixture-clip',
                                         'task_id': 'fixture-task'}}
            upload.save_sidecar({**base, 'chunk_index': 0, 'upload_id': upload_id,
                                 'state': 'done', 'phase': 'done', 'finalized': True})
            upload.save_sidecar({**base, 'chunk_index': 1, 'upload_id': 'accepted-pending-upload',
                                 'state': 'completing', 'phase': 'awaiting_finalize', 'finalized': False})
            before = {path.name: path.read_bytes() for path in journals.iterdir()}
            item = recovery.snapshot()['items'][0]
            with patch.object(upload, 'save_sidecar', wraps=upload.save_sidecar) as save, \
                 patch.object(upload, 'complete_upload') as complete, \
                 patch.object(upload, 'upload_session') as send, \
                 patch.object(upload, '_finalize_session', return_value=(True, 204)) as finalize:
                if invalid:
                    self.assertFalse(item['can_resume'], 'invalid acknowledged sibling blocks the whole group')
                    self.assertEqual(item['status'], 'needs_review')
                    with self.assertRaises(upload.UploadError):
                        upload.pump_pending(SimpleNamespace(email='mixed@example.invalid'))
                    save.assert_not_called()
                    finalize.assert_not_called()
                    self.assertEqual({path.name: path.read_bytes() for path in journals.iterdir()}, before)
                else:
                    self.assertTrue(item['can_resume'])
                    result = upload.pump_pending(SimpleNamespace(email='mixed@example.invalid'))
                    self.assertTrue(result)
                    finalize.assert_called_once()
                    self.assertIs(upload.load_sidecar('mixed-session', 1)['finalized'], True)
                complete.assert_not_called()
                send.assert_not_called()

    def test_mixed_group_with_invalid_finalized_sibling_is_not_resumable(self):
        for value in (None, '', '   ', 7, True, [], {}):
            with self.subTest(value=value):
                self.execute(value)

    def test_valid_finalized_sibling_allows_only_pending_session_finalization(self):
        self.execute('accepted-finalized-upload', invalid=False)
