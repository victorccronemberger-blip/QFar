import csv
import json
import tempfile
import unittest
from pathlib import Path

from scripts.ego4d_variety import diverse_examples, inventory


class Ego4dVarietyTests(unittest.TestCase):
    def test_examples_alternate_sources_before_repeating(self):
        rows = [{"video_uid": str(index), "video_source": "A"} for index in range(20)]
        rows.append({"video_uid": "other", "video_source": "B"})
        result = diverse_examples(rows, limit=3)
        self.assertEqual([row["video_source"] for row in result], ["A", "B", "A"])
        self.assertEqual(len({row["video_uid"] for row in result}), 3)

    def test_inventory_keeps_uncached_sensorless_and_unlabelled_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            videos = [{"video_uid": "a", "scenarios": ["Cooking", "Cooking"], "has_imu": True,
                       "duration_sec": 3600, "video_source": "A"},
                      {"video_uid": "b", "scenarios": ["Mechanic"], "has_imu": False},
                      {"video_uid": "c", "scenarios": []}]
            (root / "ego4d.json").write_text(json.dumps({"version": "2.0", "videos": videos}))
            with (root / "clips.csv").open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["exported_clip_uid", "parent_video_uid"])
                writer.writeheader()
                writer.writerow({"exported_clip_uid": "cached", "parent_video_uid": "a"})
            (root / "cached.mp4").write_bytes(b"presence only")
            report = inventory(root)
            by_name = {row["scenario"]: row for row in report["scenarios"]}
            self.assertEqual(report["unique_videos"], 3)
            self.assertEqual(by_name["Cooking"]["videos"], 1)
            self.assertEqual(by_name["Cooking"]["parents_with_local_media"], 1)
            self.assertEqual(by_name["Mechanic"]["parents_without_local_media"], 1)
            self.assertEqual(by_name["Mechanic"]["with_imu"], 0)
            self.assertIn("(sem cenário)", by_name)
