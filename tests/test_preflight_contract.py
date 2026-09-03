from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from config import AppConfig
from core.models import JobSpec
from core.preflight import run_preflight


class PreflightContractTests(unittest.TestCase):
    def test_missing_key_and_media_tools_block_startup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            report = run_preflight(
                AppConfig(
                    work_dir=root / "work",
                    ffprobe_bin="missing-ffprobe",
                    ffmpeg_bin="missing-ffmpeg",
                ),
                JobSpec(input_video=source, target_locale="ar-SA"),
            )
        self.assertFalse(report.passed)
        failed = {check.name for check in report.checks if not check.passed}
        self.assertTrue({"ffprobe", "ffmpeg", "MINIMAX_API_KEY"} <= failed)

    def test_preflight_does_not_make_remote_requests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            report = run_preflight(
                AppConfig(
                    minimax_api_key="key",
                    ffprobe_bin="ffprobe",
                    ffmpeg_bin="ffmpeg",
                ),
                JobSpec(input_video=source, target_locale="ar-SA"),
                execute_remote_checks=True,
            )
        self.assertIsNotNone(report)


if __name__ == "__main__":
    unittest.main()
