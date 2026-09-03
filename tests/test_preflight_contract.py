from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from config import AppConfig
from core.models import JobSpec
from core.preflight import run_preflight


class PreflightContractTests(unittest.TestCase):
    def test_missing_minimax_key_blocks_startup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            report = run_preflight(
                AppConfig(work_dir=root / "work", ffprobe_bin="missing-ffprobe"),
                JobSpec(input_video=source, target_locale="ar-SA"),
                execute_remote_checks=False,
            )
        self.assertFalse(report.passed)
        self.assertIn(
            "MINIMAX_API_KEY",
            {check.name for check in report.checks if not check.passed},
        )


if __name__ == "__main__":
    unittest.main()
