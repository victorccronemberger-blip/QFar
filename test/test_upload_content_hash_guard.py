"""Inert file group and session; actual upload preflight, no remote effects."""
import hashlib
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from moneymin import upload
from moneymin.upload_types import ChunkResult


class UploadContentHashGuard(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = [self.root / 'part0.mp4', self.root / 'part1.mp4']
        for i, path in enumerate(self.paths): path.write_bytes(f'inert-media-{i}'.encode())
        self.hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in self.paths]
        self.session = SimpleNamespace(email='fixture@example.invalid')

    def invoke(self, digests, **kwargs):
        return upload.upload_session(self.session, self.paths, 'fixture-org', session_id='fixture-sid',
            normalize=False, sidecar=False, persist_sidecar=False, finalize=False, evaluate=False,
            expected_video_sha256=digests, **kwargs)

    def test_last_file_mismatch_blocks_entire_group_before_effects(self):
        self.paths[-1].write_bytes(b'inert-media-Z')
        with patch.object(upload, '_upload_single_chunk') as chunk, patch.object(upload, 'save_sidecar') as journal:
            with self.assertRaises(upload.UploadError): self.invoke(self.hashes)
        chunk.assert_not_called(); journal.assert_not_called()

    def test_digest_shape_and_normalization_are_rejected_before_effects(self):
        for digests in [True, ['0' * 64], ['Z' * 64, self.hashes[1]], [None, self.hashes[1]]]:
            with self.subTest(digests=repr(digests)):
                with patch.object(upload, '_upload_single_chunk') as chunk:
                    with self.assertRaises(upload.UploadError): self.invoke(digests)
                chunk.assert_not_called()
        with self.assertRaises(upload.UploadError):
            upload.upload_session(self.session, self.paths[0], 'fixture-org',
                normalize=True, expected_video_sha256=self.hashes[0])

    def test_healthy_group_forwards_exact_digest_without_new_transport(self):
        def finish(**kwargs):
            index = kwargs['chunk_index']
            self.assertEqual(kwargs['_expected_video_sha256'], self.hashes[index])
            return ChunkResult(upload_id=f'fixture-{index}', chunk_index=index,
                log_id=f'fixture-sid_{index}', blob_path='', size_bytes=13, duration_ms=3000, state='done')
        with patch.object(upload, '_probe_duration_ms', return_value=3000), \
             patch.object(upload, '_upload_single_chunk', side_effect=finish) as chunk:
            result = self.invoke(self.hashes)
        self.assertEqual(len(result.chunks), 2); self.assertEqual(chunk.call_count, 2)


if __name__ == '__main__': unittest.main()
