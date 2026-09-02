"""Seedance content construction for the v2 audio-first pipeline."""

from __future__ import annotations

from collections.abc import Iterable

from core.models import JobSpec, UploadedAsset
from utils.errors import ValidationError


SEEDANCE_PROMPT_VERSION = "v2"


def _https(value: str, label: str) -> str:
    if not value.startswith("https://"):
        raise ValidationError(f"{label} must be an HTTPS URL")
    return value


def build_seedance_content(
    source_url: str,
    localized_audio_url: str,
    reference_assets: Iterable[UploadedAsset],
    job: JobSpec,
) -> list[dict[str, object]]:
    content: list[dict[str, object]] = [
        {
            "type": "text",
            "text": (
                f"严格参考视频1的镜头结构、机位、动作节奏、故事内容和剪辑节奏。"
                f"将人物外观和场景环境本地化为{job.target_region}（{job.target_locale}）版本。"
                "人物对白口型严格跟随音频1。不要新增角色，不改变角色数量，"
                "不改变主要动作，不改变镜头顺序。使用音频1作为唯一音频条件，"
                "不要依赖或输出新的对白音轨。"
            ),
        },
        {
            "type": "video_url",
            "video_url": {"url": _https(source_url, "source video")},
            "role": "reference_video",
        },
        {
            "type": "audio_url",
            "audio_url": {"url": _https(localized_audio_url, "localized audio")},
            "role": "reference_audio",
        },
    ]
    for asset in reference_assets:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": _https(asset.remote_url, "reference image")},
                "role": "reference_image",
            }
        )
    return content
