from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from moneymin import holoassist


class HoloAssistSelfHealTests(unittest.TestCase):
    def test_annotations_download_themselves_when_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "annotations.json"

            def prepare_metadata(*_args, **_kwargs):
                path.write_text(json.dumps([{"video_name": "sample"}]), encoding="utf-8")
                return path, Path(directory) / "splits.zip"

            with mock.patch.object(holoassist, "annotations_path", return_value=path), \
                 mock.patch.object(holoassist, "download_metadata", side_effect=prepare_metadata) as download:
                records = holoassist.annotations()

            download.assert_called_once_with()
            self.assertEqual(records[0]["video_name"], "sample")


if __name__ == "__main__":
    unittest.main()
