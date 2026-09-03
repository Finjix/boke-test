"""Prompt and inline multimodal content builders for MiniMax H3."""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from pathlib import Path

from config import (
    MINIMAX_IMAGE_MAX_FILE_BYTES,
    MINIMAX_MAX_REQUEST_BYTES,
    MINIMAX_VIDEO_MAX_FILE_BYTES,
)
from language_config import TargetLocale
from utils.errors import ValidationError


MAX_H3_PROMPT_CHARS = 7000
MAX_CONTEXT_IR_ANALYSIS_CHARS = 6000
IMAGE_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
}


def _validate_prompt(
    prompt: str,
    label: str,
    *,
    preserve: bool = False,
    max_chars: int = MAX_H3_PROMPT_CHARS,
) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValidationError(f"{label}不能为空")
    value = prompt if preserve else prompt.strip()
    if len(value) > max_chars:
        raise ValidationError(f"{label}超过 {max_chars} 字符限制，请重试")
    return value


def _data_url(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("data:"):
        raise ValidationError(f"{label}必须是 Base64 数据地址")
    return value


def _scene_localization_rules(locale: TargetLocale) -> list[str]:
    """Return the non-negotiable scene requirements for both H3 requests."""

    return [
        "【本地化硬性验收规则】本地化优先级高于复刻源视频的背景外观；源视频只用于保留故事、表演、动作、镜头和时序。",
        (
            "原始视频是动作、人物与物体位置、站位、姿势、相对距离、相机机位与运动、"
            "景别、构图、镜头时序和剪辑节奏的唯一依据；这些内容必须严格按原始视频执行。"
        ),
        (
            "逐镜头将所有可见环境重建为目标地区版本：建筑外观与室内风格、街景、道路设施、"
            "车辆与车牌、招牌、菜单、包装、屏幕、道具、服装、光线和生活细节都必须本地化。"
        ),
        (
            "可以保持相机机位、构图和空间关系，但不得直接沿用源视频中可辨识的建筑样式、"
            "街道设施、商店外观或环境文字。"
        ),
        (
            f"画面中每一处可辨识的文字都必须使用「{locale.language}」；"
            "不得保留或生成源语言文字、拉丁字母招牌、原始品牌字样或无意义字符。"
        ),
        (
            "如果结果只替换人物、服装或对白，而背景建筑、街景和文字仍与源视频相同，"
            "则视为未完成本地化，必须重新生成该镜头。"
        ),
    ]


def build_context_ir_prompt(
    locale: TargetLocale,
    *,
    has_reference_images: bool = False,
) -> str:
    lines = [
        f"请将源视频完整本地化为「{locale.region}（{locale.language}）」版本。",
        "原始视频是动作、位置和镜头的唯一依据，必须严格保持故事、人物关系、动作表演、镜头顺序、相机机位、剪辑节奏和转场。",
    ]
    lines.extend(_scene_localization_rules(locale))
    if has_reference_images:
        lines.append(
            "全部参考图仅用于人物、服装、材质、色彩与整体美术的外观风格参考，"
            "绝不能复制或推断其中的动作、姿势、人物位置、物体位置、构图、相机机位、"
            "镜头运动、时序或剪辑；如与原始视频冲突，必须完全以原始视频为准。"
        )
    lines.extend(
        [
            (
                "只输出可直接交给 H3 的精简镜头结构提示词：覆盖全部镜头，"
                f"删除重复解释和无关修辞，全文不得超过 {MAX_CONTEXT_IR_ANALYSIS_CHARS} 个字符。"
            ),
            "源视频是动作、时序和表演的主要依据。",
            "输出应保持源视频节奏，并生成同步的目标语言音频。",
        ]
    )
    return "\n".join(lines)


def build_generation_prompt(locale: TargetLocale, enhanced_prompt: str) -> str:
    """Keep Context-IR analysis while restoring hard rules in the H3 call."""

    analysis = _validate_prompt(
        enhanced_prompt,
        "结构提示词",
        max_chars=MAX_CONTEXT_IR_ANALYSIS_CHARS,
    )
    rules = "\n".join(_scene_localization_rules(locale))
    prefix = "以下是 Context-IR 的镜头分析，请据此生成视频：\n"
    suffix = f"\n\n无论上述分析如何表述，以下规则不得省略或降级：\n{rules}"
    return _validate_prompt(
        f"{prefix}{analysis}{suffix}",
        "最终提示词",
    )


def file_data_url(
    path: Path,
    *,
    kind: str,
    label: str,
) -> str:
    path = Path(path)
    if not path.is_file():
        raise ValidationError(f"{label}不存在")
    suffix = path.suffix.lower()
    if kind == "image":
        mime = IMAGE_MIME_TYPES.get(suffix)
        limit = MINIMAX_IMAGE_MAX_FILE_BYTES
    elif kind == "video":
        if suffix != ".mp4":
            raise ValidationError("视频临时文件必须是 MP4")
        mime = "video/mp4"
        limit = MINIMAX_VIDEO_MAX_FILE_BYTES
    else:
        raise ValidationError(f"不支持的媒体类型: {kind}")
    if mime is None:
        raise ValidationError(f"{label}格式不支持")
    if path.stat().st_size > limit:
        limit_mib = limit // (1024 * 1024)
        raise ValidationError(f"{label}不能超过 {limit_mib} MB")
    try:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError as exc:
        raise ValidationError(f"{label}读取失败") from exc
    return f"data:{mime};base64,{encoded}"


def _image_item(data_url: str) -> dict[str, object]:
    return {
        "type": "image_url",
        "image_url": {"url": data_url},
        "role": "reference_image",
    }


def _video_item(data_url: str) -> dict[str, object]:
    return {
        "type": "video_url",
        "video_url": {"url": data_url},
        "role": "reference_video",
    }


def build_multimodal_content(
    video_data_url: str,
    prompt: str,
    *,
    reference_image_data_urls: Sequence[str] | None = None,
) -> list[dict[str, object]]:
    content: list[dict[str, object]] = [
        {"type": "text", "text": _validate_prompt(prompt, "提示词")}
    ]
    for reference_image_data_url in reference_image_data_urls or ():
        if reference_image_data_url:
            content.append(_image_item(reference_image_data_url))
    content.append(_video_item(_data_url(video_data_url, "视频数据")))
    return content


def build_context_ir_content(
    source_video_url: str,
    requirement: str,
    *,
    reference_image_urls: Sequence[str] | None = None,
) -> list[dict[str, object]]:
    return build_multimodal_content(
        source_video_url,
        requirement,
        reference_image_data_urls=reference_image_urls,
    )


def build_video_content(
    source_video_url: str,
    enhanced_prompt: str,
    *,
    reference_image_urls: Sequence[str] | None = None,
) -> list[dict[str, object]]:
    return build_multimodal_content(
        source_video_url,
        enhanced_prompt,
        reference_image_data_urls=reference_image_urls,
    )


def payload_size_bytes(payload: dict[str, object]) -> int:
    return len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def ensure_payload_size(payload: dict[str, object]) -> None:
    if payload_size_bytes(payload) > MINIMAX_MAX_REQUEST_BYTES:
        raise ValidationError("参考素材过大，请将请求体控制在 64 MB 以内")
