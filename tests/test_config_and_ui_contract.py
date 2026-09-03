from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from config import AppConfig
from core.h3_prompt import build_context_ir_prompt
from core.models import JobSpec
from language_config import locale_from_code
from utils.settings_store import SettingsStore


class ConfigAndUiContractTests(unittest.TestCase):
    def test_only_minimax_key_is_a_runtime_requirement(self) -> None:
        config = AppConfig.from_env(
            {
                "UNUSED_PROVIDER_KEY": "ignored",
                "UNUSED_PROVIDER_MODEL": "ignored",
                "MINIMAX_RESOLUTION": "2K",
            },
            base_dir=Path.cwd(),
            load_file=False,
        )
        self.assertEqual(config.missing_runtime_values(), ["MINIMAX_API_KEY"])
        self.assertEqual(config.minimax_resolution, "2K")
        self.assertEqual(set(config.__dataclass_fields__), {
            "minimax_api_key",
            "minimax_base_url",
            "minimax_model",
            "minimax_task_timeout",
            "minimax_resolution",
            "uguu_upload_url",
            "uguu_max_file_mib",
            "uguu_expire_hours",
            "http_timeout",
            "poll_interval",
            "work_dir",
            "ffprobe_bin",
        })

    def test_job_spec_derives_locale_data_from_preset_code(self) -> None:
        spec = JobSpec(input_video=Path("source.mp4"), target_locale="ar-SA")
        locale = locale_from_code(spec.target_locale)
        self.assertIsNotNone(locale)
        self.assertEqual(locale.language, "Arabic")
        self.assertEqual(locale.region, "Gulf")

    def test_context_ir_prompt_contains_fixed_region_and_audio_requirements(self) -> None:
        locale = locale_from_code("ar-SA")
        self.assertIsNotNone(locale)
        prompt = build_context_ir_prompt(locale)
        self.assertIn("「Gulf（Arabic）」", prompt)
        self.assertIn("故事、人物与关系、动作、镜头顺序", prompt)
        self.assertIn("带同步目标语音或音频", prompt)

    def test_settings_store_drops_legacy_values_and_keeps_only_minimax_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = SettingsStore(path)
            store.save(
                {
                    "obsolete_key": "legacy",
                    "obsolete_model": "legacy",
                    "minimax_api_key": "current",
                }
            )
            self.assertEqual(store.load(), {"minimax_api_key": "current"})
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("preferences", payload)
            self.assertEqual(set(payload["values"]), {"minimax_api_key"})

    def test_tkinter_surface_has_only_current_task_controls(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "ui" / "window.py").read_text(
            encoding="utf-8"
        )
        for label in ("开始处理", "打开输出目录"):
            self.assertIn(label, source)
        self.assertIn("3–15 秒", source)
        for removed in (
            'text="上传下一片"',
            'text="完成拼接"',
            "append_button",
            "finish_button",
            "LogPanel",
            "实时日志",
            "history_store",
            "approve_",
            "retry_",
            "continue_",
        ):
            self.assertNotIn(removed, source)


if __name__ == "__main__":
    unittest.main()
