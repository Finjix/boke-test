from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
import tempfile
import unittest

from config import AppConfig
from core.h3_prompt import build_context_ir_prompt
from core.models import JobSpec
from language_config import locale_from_code
from utils.settings_store import SettingsStore


class ConfigAndUiContractTests(unittest.TestCase):
    def test_config_has_only_current_runtime_settings(self) -> None:
        config = AppConfig.from_env(
            {
                "MINIMAX_RESOLUTION": "2K",
                "OLD_PROVIDER_KEY": "ignored",
            },
            base_dir=Path.cwd(),
            load_file=False,
        )
        self.assertEqual(config.missing_runtime_values(), ["MINIMAX_API_KEY"])
        self.assertEqual(config.minimax_resolution, "2K")
        self.assertEqual(
            {item.name for item in fields(config)},
            {
                "minimax_api_key",
                "minimax_base_url",
                "minimax_model",
                "minimax_task_timeout",
                "minimax_resolution",
                "http_timeout",
                "poll_interval",
                "work_dir",
                "output_dir",
                "ffprobe_bin",
                "ffmpeg_bin",
            },
        )

    def test_job_spec_contains_one_video_and_two_optional_images(self) -> None:
        spec = JobSpec(
            input_video=Path("source.mp4"),
            person_image=Path("person.png"),
            scene_image=Path("scene.jpg"),
            target_locale="ar-SA",
        )
        self.assertEqual(spec.input_video, Path("source.mp4"))
        self.assertEqual(spec.person_image, Path("person.png"))
        self.assertEqual(spec.scene_image, Path("scene.jpg"))
        self.assertEqual(locale_from_code(spec.target_locale).region, "Gulf")

    def test_prompt_mentions_region_and_available_reference_roles(self) -> None:
        locale = locale_from_code("ar-SA")
        self.assertIsNotNone(locale)
        prompt = build_context_ir_prompt(
            locale,
            has_person_image=True,
            has_scene_image=True,
        )
        self.assertIn("Gulf（Arabic）", prompt)
        self.assertIn("人物参考图", prompt)
        self.assertIn("场景参考图", prompt)

    def test_settings_store_keeps_only_the_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = SettingsStore(path)
            store.save({"old": "removed", "minimax_api_key": "current"})
            self.assertEqual(store.load(), {"minimax_api_key": "current"})
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(payload["values"]), {"minimax_api_key"})

    def test_gui_surface_is_minimal(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "ui" / "window.py"
        ).read_text(encoding="utf-8")
        for label in ("开始处理", "打开 output", "人物图", "场景图", "目标地区"):
            self.assertIn(label, source)
        for removed in (
            "append_segment",
            "concat_videos",
            "history",
            "实时日志",
            "task ID",
            "prompt_text",
            "Uguu",
            "retry_",
            "resume",
        ):
            self.assertNotIn(removed, source)


if __name__ == "__main__":
    unittest.main()
