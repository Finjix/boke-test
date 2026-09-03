"""Prompt and multimodal content builders for MiniMax H3."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from language_config import TargetLocale
from utils.errors import ValidationError


CONTEXT_IR_PROMPT_VERSION = "context-ir-v1"
MAX_H3_PROMPT_CHARS = 7000


def _https(value: str, label: str) -> str:
    if not isinstance(value, str) or urlparse(value).scheme != "https":
        raise ValidationError(f"{label} must be an HTTPS URL")
    return value


def _validate_prompt(prompt: str, label: str, *, preserve: bool = False) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValidationError(f"{label} cannot be empty")
    value = prompt if preserve else prompt.strip()
    if len(value) > MAX_H3_PROMPT_CHARS:
        raise ValidationError(
            f"{label} exceeds the {MAX_H3_PROMPT_CHARS}-character limit"
        )
    return value


def build_context_ir_prompt(locale: TargetLocale) -> str:
    """Build the only user-facing requirement sent to Context-IR."""

    return (
        f"请将提供的源视频完整本地化为「{locale.region}（{locale.language}）」版本。\n"
        "保持源视频的故事、人物与关系、动作、镜头顺序、构图、剪辑节奏、转场和整体创意不变；"
        "将与目标地区相关的场景环境、建筑、服装、道具、招牌、包装文字、对白和语音自然转换为该地区版本。\n"
        "源视频是运动、时序和表演的主要依据。\n"
        "输出与输入片段时长一致、带同步目标语音或音频的视频。"
    )


def build_context_ir_content(source_video_url: str, requirement: str) -> list[dict[str, Any]]:
    return [
        {"type": "text", "text": _validate_prompt(requirement, "Context-IR prompt")},
        {
            "type": "video_url",
            "video_url": {"url": _https(source_video_url, "Context-IR source video")},
            "role": "reference_video",
        },
    ]


def build_video_content(source_video_url: str, enhanced_prompt: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "text",
            "text": _validate_prompt(enhanced_prompt, "H3 prompt", preserve=True),
        },
        {
            "type": "video_url",
            "video_url": {"url": _https(source_video_url, "H3 source video")},
            "role": "reference_video",
        },
    ]
