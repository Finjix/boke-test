from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from config import AppConfig
from core.models import JobSpec
from core.preflight import run_preflight


class FakeMedia:
    def doctor(self, **kwargs):
        return "ok"

    def schema(self, *args, **kwargs):
        return {"ok": True}


class FakeProvider:
    def check_access(self, **kwargs):
        return None


class PreflightTests(unittest.TestCase):
    def test_static_preflight_reports_missing_runtime_dependency_and_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.mp4"
            source.write_bytes(b"video")
            config = AppConfig(
                ffmpeg_bin=sys.executable,
                ffprobe_bin=sys.executable,
                mediakit_cli_bin="definitely-not-installed",
                work_dir=Path(directory) / "work",
            )
            report = run_preflight(
                config,
                JobSpec(
                    input_video=source,
                    target_language="English",
                    target_region="United States",
                ),
                clients={
                    "mediakit": FakeMedia(),
                    "ark": FakeProvider(),
                    "seed_audio": FakeProvider(),
                    "seedance": FakeProvider(),
                    "uguu": FakeProvider(),
                },
                execute_remote_checks=False,
            )
            self.assertFalse(report.passed)
            names = {check.name for check in report.checks if not check.passed}
            self.assertIn("mediakit-cli", names)
            self.assertIn("ARK_API_KEY", names)

    def test_static_preflight_can_pass_with_available_executable_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.mp4"
            source.write_bytes(b"video")
            config = AppConfig(
                ark_api_key="ark",
                mediakit_api_key="media",
                seed_audio_api_key="audio",
                seedance_model_id="ep-test",
                ffmpeg_bin=sys.executable,
                ffprobe_bin=sys.executable,
                mediakit_cli_bin=sys.executable,
                work_dir=Path(directory) / "work",
            )
            report = run_preflight(
                config,
                JobSpec(
                    input_video=source,
                    target_language="English",
                    target_region="United States",
                ),
                clients={"mediakit": FakeMedia()},
                execute_remote_checks=False,
            )
            self.assertTrue(report.passed)

