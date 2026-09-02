from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from config import AppConfig
from core.models import JobSpec
from core.preflight import run_preflight


def _spec(source: Path) -> JobSpec:
    return JobSpec(
        input_video=source,
        target_language="en",
        target_region="United States",
        target_locale="en-US",
    )


class FakeProvider:
    def check_access(self, **kwargs):
        return None


class PreflightTests(unittest.TestCase):
    def test_static_preflight_reports_missing_runtime_dependency_and_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.mp4"
            source.write_bytes(b"video")
            config = AppConfig(
                ffprobe_bin="also-not-installed",
                work_dir=Path(directory) / "work",
            )
            report = run_preflight(
                config,
                _spec(source),
                clients={
                    "seedance": FakeProvider(),
                    "uguu": FakeProvider(),
                },
                execute_remote_checks=False,
            )
            self.assertFalse(report.passed)
            names = {check.name for check in report.checks if not check.passed}
            self.assertIn("ffprobe", names)
            self.assertIn("ARK_API_KEY", names)
            self.assertIn("SEEDANCE_MODEL_ID", names)
            self.assertNotIn("ffmpeg", {check.name for check in report.checks})
            self.assertNotIn("SEED_AUDIO_API_KEY", {check.name for check in report.checks})

    def test_static_preflight_can_pass_without_model_generation_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.mp4"
            source.write_bytes(b"video")
            config = AppConfig(
                ark_api_key="ark",
                seedance_model_id="ep-test",
                ffprobe_bin=sys.executable,
                work_dir=Path(directory) / "work",
            )
            report = run_preflight(
                config,
                _spec(source),
                clients={
                    "seedance": FakeProvider(),
                    "uguu": FakeProvider(),
                },
                execute_remote_checks=False,
            )
            self.assertTrue(report.passed)
            self.assertIn("DOUBAO_MODEL", {check.name for check in report.checks})
