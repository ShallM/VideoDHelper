import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from common import UserError
from media import executable


class DistributionTests(unittest.TestCase):
    def test_bundled_tool_wins_over_developer_path(self):
        with tempfile.TemporaryDirectory() as directory:
            tool = Path(directory) / "ffmpeg"
            tool.write_text("#!/bin/sh\nexit 0\n")
            tool.chmod(0o755)
            with patch.dict(os.environ, {"VIDEOHELPER_BUNDLED_TOOLS": directory}):
                with patch("media.shutil.which", side_effect=AssertionError("Should not inspect developer PATH")):
                    self.assertEqual(executable("ffmpeg"), str(tool))

    def test_incomplete_bundle_does_not_fall_back_to_homebrew(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"VIDEOHELPER_BUNDLED_TOOLS": directory}):
                with self.assertRaises(UserError):
                    executable("ffmpeg")
