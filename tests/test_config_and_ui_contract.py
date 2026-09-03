from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
import tempfile
import unittest

from config import MINIMAX_MAX_REFERENCE_IMAGES
from config import AppConfig
from core.h3_prompt import (
    MAX_CONTEXT_IR_ANALYSIS_CHARS,
    MAX_H3_PROMPT_CHARS,
    build_context_ir_prompt,
    build_generation_prompt,
)
from core.models import JobSpec
from language_config import locale_from_code
from ui.window import append_reference_images
from utils.errors import ValidationError
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

    def test_job_spec_contains_one_video_and_multiple_optional_reference_images(self) -> None:
        spec = JobSpec(
            input_video=Path("source.mp4"),
            reference_images=(Path("reference-1.png"), Path("reference-2.png")),
            target_locale="ar-SA",
        )
        self.assertEqual(spec.input_video, Path("source.mp4"))
        self.assertEqual(
            spec.reference_images,
            (Path("reference-1.png"), Path("reference-2.png")),
        )
        self.assertEqual(locale_from_code(spec.target_locale).region, "Gulf")

    def test_job_spec_rejects_more_than_nine_reference_images(self) -> None:
        with self.assertRaisesRegex(ValueError, "参考图最多上传 9 张"):
            JobSpec(
                input_video=Path("source.mp4"),
                reference_images=tuple(
                    Path(f"reference-{index}.png")
                    for index in range(MINIMAX_MAX_REFERENCE_IMAGES + 1)
                ),
                target_locale="ar-SA",
            )

    def test_reference_images_can_be_added_across_multiple_selections(self) -> None:
        initial = (Path("first.png"),)
        result = append_reference_images(
            initial,
            (Path("second.png"), Path("third.png"), Path("first.png")),
        )
        self.assertEqual(
            result,
            (Path("first.png"), Path("second.png"), Path("third.png")),
        )
        with self.assertRaisesRegex(ValueError, "最多添加 9 张"):
            append_reference_images(
                result,
                tuple(Path(f"image-{index}.png") for index in range(7)),
            )

    def test_prompt_requires_full_scene_and_text_localization(self) -> None:
        locale = locale_from_code("ar-SA")
        self.assertIsNotNone(locale)
        prompt = build_context_ir_prompt(
            locale,
            has_reference_images=True,
        )
        self.assertIn("Gulf（Arabic）", prompt)
        self.assertIn("镜头双轨契约", prompt)
        self.assertIn("逐镜头将所有可见环境重建", prompt)
        self.assertIn("每一处可辨识的文字都必须使用「Arabic」", prompt)
        self.assertIn("不得保留或生成源语言文字、拉丁字母招牌", prompt)
        self.assertIn("镜头双轨契约", prompt)
        self.assertIn("时空连续性轨", prompt)
        self.assertIn("视觉本地化轨", prompt)
        self.assertIn("同一批人物、服装、道具、材质、环境和可辨识文字", prompt)
        self.assertIn("不得把源视频的地域视觉特征当作默认保留项", prompt)
        self.assertIn("若只翻译了文字、车牌或语音", prompt)
        self.assertIn("参考图只为视觉本地化轨提供", prompt)
        self.assertIn("不能复制或推断其中的动作、姿势、人物位置", prompt)
        self.assertIn("所有时空信息始终完全以原始视频为准", prompt)
        self.assertIn(
            f"不得超过 {MAX_CONTEXT_IR_ANALYSIS_CHARS} 个字符",
            prompt,
        )

    def test_generation_prompt_restores_hard_rules_after_context_ir(self) -> None:
        locale = locale_from_code("ar-SA")
        self.assertIsNotNone(locale)
        prompt = build_generation_prompt(locale, "Context-IR returned analysis")
        self.assertIn("Context-IR returned analysis", prompt)
        self.assertIn("无论上述分析如何表述", prompt)
        self.assertIn("镜头双轨契约", prompt)
        self.assertIn("不得保留或生成源语言文字、拉丁字母招牌", prompt)
        self.assertIn("时空连续性轨", prompt)
        self.assertIn("视觉本地化轨", prompt)

    def test_generation_prompt_rejects_overlong_context_ir_without_truncation(self) -> None:
        locale = locale_from_code("ar-SA")
        self.assertIsNotNone(locale)
        analysis = "镜头分析" * (MAX_CONTEXT_IR_ANALYSIS_CHARS + 1)
        with self.assertRaisesRegex(
            ValidationError,
            f"结构提示词超过 {MAX_CONTEXT_IR_ANALYSIS_CHARS} 字符限制，请重试",
        ):
            build_generation_prompt(locale, analysis)

    def test_maximum_length_context_ir_leaves_room_for_hard_rules(self) -> None:
        locale = locale_from_code("ar-SA")
        self.assertIsNotNone(locale)
        prompt = build_generation_prompt(locale, "镜头" * (MAX_CONTEXT_IR_ANALYSIS_CHARS // 2))
        self.assertLessEqual(len(prompt), MAX_H3_PROMPT_CHARS)

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
        settings_source = (
            Path(__file__).resolve().parents[1] / "ui" / "settings.py"
        ).read_text(encoding="utf-8")
        for label in ("开始处理", "打开输出目录", "参考图（可选）", "目标地区"):
            self.assertIn(label, source)
        self.assertIn('textvariable=self.status_var', source)
        self.assertIn('状态：', source)
        self.assertIn('text="Finjix 钟丰骏制作"', source)
        self.assertIn('geometry("760x290")', source)
        self.assertIn('minsize(700, 280)', source)
        self.assertIn("root.rowconfigure(5, weight=1)", source)
        self.assertIn("text=\"Finjix 钟丰骏制作\"", source)
        self.assertIn("row=7, column=0, columnspan=4", source)
        self.assertIn("row=0, column=1, columnspan=3", source)
        self.assertNotIn("Progressbar", source)
        self.assertNotIn("progress_var", source)
        self.assertNotIn("LabelFrame", source)
        self.assertNotIn('text="输出"', source)
        self.assertNotIn("output_var", source)
        self.assertIn("row=4, column=1", source)
        self.assertIn("row=4, column=2", source)
        self.assertIn("self.settings.grid(row=3", source)
        self.assertIn("askopenfilenames", source)
        self.assertIn("MINIMAX_MAX_REFERENCE_IMAGES", source)
        self.assertIn('button_text="添加"', source)
        self.assertIn("append_reference_images", source)
        self.assertIn('text="拼接视频"', source)
        self.assertIn('text="清除"', source)
        self.assertIn("_clear_video", source)
        self.assertIn("_clear_reference_images", source)
        self.assertIn("ConcatenateWindow", source)
        self.assertIn("column=3", source)
        self.assertIn('text="目标地区"', source)
        self.assertIn('text="MiniMax API Key"', settings_source)
        self.assertNotIn("LabelFrame", settings_source)
        for removed in (
            "append_segment",
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
