import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moneymin import campaign, holoassist
from moneymin.web import server


class ProviderCacheCleanupTests(unittest.TestCase):
    def test_provider_cleanup_keeps_other_provider_and_catalog(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ego = root / "ego4d"
            recordings = root / "holoassist" / "recordings" / "clip"
            ego.mkdir()
            recordings.mkdir(parents=True)
            ego_video = ego / "ego_clip.mp4"
            holo_video = ego / "holoassist_clip_native.mp4"
            holo_source = ego / "holoassist_clip_native.mp4.source.json"
            catalog = ego / "catalog.json"
            recording = recordings / "video.mp4"
            for path in (ego_video, holo_video, holo_source, catalog, recording):
                path.write_bytes(b"123")
            with patch.object(holoassist, "data_dir", return_value=root / "holoassist"):
                result = campaign.cleanup_media_cache(ego, provider="holoassist")
                self.assertEqual(result["files"], 3)
                self.assertTrue(ego_video.exists())
                self.assertTrue(catalog.exists())
                self.assertFalse(holo_video.exists())
                self.assertFalse(recording.exists())
                result = campaign.cleanup_media_cache(ego, provider="ego4d")
                self.assertEqual(result["files"], 1)
                self.assertFalse(ego_video.exists())
                self.assertTrue(catalog.exists())

    def test_local_opt_out_blocks_holo_and_maps_combined_to_ego(self):
        with patch.object(server, "_load_prefs", return_value={"holoassist_enabled": False}):
            self.assertEqual(server._local_dataset_provider("all"), "ego4d")
            with self.assertRaises(ValueError):
                server._local_dataset_provider("holoassist")
            response = server.create_app().test_client().post(
                "/api/holo-cache/start", json={"provider": "holoassist"})
            self.assertEqual(response.status_code, 400)

    def test_cleanup_api_requires_known_provider_and_passes_selection(self):
        client = server.create_app().test_client()
        self.assertEqual(client.post("/api/storage/cleanup", json={"provider": "other"}).status_code, 400)
        with patch.object(server.campaign, "cleanup_media_cache", return_value={
            "files": 2, "bytes": 10, "errors": [], "skipped": 0,
        }) as cleanup:
            response = client.post("/api/storage/cleanup", json={"provider": "holoassist"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["provider"], "holoassist")
        self.assertEqual(cleanup.call_args.kwargs["provider"], "holoassist")
