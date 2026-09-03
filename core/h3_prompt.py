"""Prompt and inline multimodal content builders for MiniMax H3."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from config import (
    MINIMAX_IMAGE_MAX_FILE_BYTES,
    MINIMAX_MAX_REQUEST_BYTES,
    MINIMAX_VIDEO_MAX_FILE_BYTES,
)
from language_config import TargetLocale
from utils.errors import ValidationError


MAX_H3_PROMPT_CHARS = 7000
IMAGE_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
}


def _validate_prompt(prompt: str, label: str, *, preserve: bool = False) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValidationError(f"{label}不能为空")
    value = prompt if preserve else prompt.strip()
    if len(value) > MAX_H3_PROMPT_CHARS:
        raise ValidationError(f"{label}超过 {MAX_H3_PROMPT_CHARS} 字符限制")
    return value


def _data_url(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("data:"):
        raise ValidationError(f"{label}必须是 Base64 数据地址")
    return value


def build_context_ir_prompt(
    locale: TargetLocale,
    *,
    has_person_image: bool = False,
    has_scene_image: bool = False,
) -> str:
    lines = [
        f"请将源视频完整本地化为「{locale.region}（{locale.language}）」版本。",
        "保持故事、人物关系、动作表演、镜头顺序、构图、剪辑节奏和转场不变。",
        "将建筑、街景、招牌、包装、服装、道具、灯光、对白和语音自然转换为目标地区版本。",
    ]
    if has_person_image:
        lines.append("人物参考图用于保持人物外观、身份和连续性。")
    if has_scene_image:
        lines.append("场景参考图用于指导目标地区的环境、建筑和美术风格。")
    lines.extend(
        [
            "源视频是动作、时序和表演的主要依据。",
            "输出应保持源视频节奏，并生成同步的目标语言音频。",
        ]
    )
    return "\n".join(lines)


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
    person_image_data_url: str | None = None,
    scene_image_data_url: str | None = None,
) -> list[dict[str, object]]:
    content: list[dict[str, object]] = [
        {"type": "text", "text": _validate_prompt(prompt, "提示词")}
    ]
    if person_image_data_url:
        content.append(_image_item(person_image_data_url))
    if scene_image_data_url:
        content.append(_image_item(scene_image_data_url))
    content.append(_video_item(_data_url(video_data_url, "视频数据")))
    return content


def build_context_ir_content(
    source_video_url: str,
    requirement: str,
    *,
    person_image_url: str | None = None,
    scene_image_url: str | None = None,
) -> list[dict[str, object]]:
    return build_multimodal_content(
        source_video_url,
        requirement,
        person_image_data_url=person_image_url,
        scene_image_data_url=scene_image_url,
    )


def build_video_content(
    source_video_url: str,
    enhanced_prompt: str,
    *,
    person_image_url: str | None = None,
    scene_image_url: str | None = None,
) -> list[dict[str, object]]:
    return build_multimodal_content(
        source_video_url,
        enhanced_prompt,
        person_image_data_url=person_image_url,
        scene_image_data_url=scene_image_url,
    )


def payload_size_bytes(payload: dict[str, object]) -> int:
    return len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def ensure_payload_size(payload: dict[str, object]) -> None:
    if payload_size_bytes(payload) > MINIMAX_MAX_REQUEST_BYTES:
        raise ValidationError("参考素材过大，请将请求体控制在 64 MB 以内")
