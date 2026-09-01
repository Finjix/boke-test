"""Speaker keyframe selection and profile response validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.models import Segment, SpeakerProfile
from utils.errors import ValidationError
from utils.json_parser import parse_strict_json


@dataclass(frozen=True)
class SpeakerAnchor:
    speaker_id: str
    segment_id: str
    start: float
    middle: float
    end: float


def select_speaker_anchors(
    segments: list[Segment],
    source_duration: float,
    *,
    max_segments_per_speaker: int = 3,
) -> list[SpeakerAnchor]:
    if source_duration <= 0:
        raise ValidationError("source duration must be positive")
    grouped: dict[str, list[Segment]] = {}
    for segment in segments:
        grouped.setdefault(segment.speaker, []).append(segment)

    anchors: list[SpeakerAnchor] = []
    for speaker_id, speaker_segments in grouped.items():
        for segment in speaker_segments[:max_segments_per_speaker]:
            start = max(0.0, min(segment.start, source_duration))
            end = max(start, min(segment.end, source_duration))
            middle = (start + end) / 2
            anchors.append(
                SpeakerAnchor(
                    speaker_id=speaker_id,
                    segment_id=segment.id,
                    start=start,
                    middle=middle,
                    end=end,
                )
            )
    return anchors


def build_speaker_analysis_messages(
    speaker_id: str,
    frame_urls: list[str],
) -> list[dict[str, object]]:
    if not frame_urls:
        raise ValidationError(f"no keyframes available for {speaker_id}")
    content: list[dict[str, object]] = [
        {
            "type": "image_url",
            "image_url": {"url": url},
        }
        for url in frame_urls
    ]
    content.append(
        {
            "type": "text",
            "text": (
                f"这些图片来自 {speaker_id} 正在说话的时间段。判断最可能正在说话角色的"
                "性别、年龄段、角色类型和可用于配音的声音风格。不要识别真实身份。"
                "只输出 JSON，speaker_id 必须保持不变。"
            ),
        }
    )
    return [
        {"role": "system", "content": "你是视频角色分析器，只输出 JSON。"},
        {"role": "user", "content": content},
    ]


def _enum(value: Any, allowed: set[str], default: str = "unknown") -> str:
    value = str(value or "").strip().lower()
    return value if value in allowed else default


def parse_speaker_profile(speaker_id: str, content: str) -> SpeakerProfile:
    value = parse_strict_json(content, description=f"{speaker_id} role analysis")
    if not isinstance(value, dict):
        raise ValidationError(f"{speaker_id} role analysis must be a JSON object")
    styles = value.get("voice_style", ["neutral"])
    if isinstance(styles, str):
        styles = [styles]
    if not isinstance(styles, list):
        styles = ["neutral"]
    try:
        return SpeakerProfile(
            speaker_id=speaker_id,
            gender=_enum(value.get("gender"), {"male", "female", "unknown"}),
            age_group=_enum(
                value.get("age_group"),
                {"child", "young", "middle", "elderly", "unknown"},
            ),
            role_type=_enum(
                value.get("role_type"),
                {"main_character", "supporting", "narrator", "unknown"},
            ),
            voice_style=[str(item) for item in styles],
            confidence=(
                float(value["confidence"])
                if value.get("confidence") is not None
                else None
            ),
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"invalid profile for {speaker_id}: {exc}") from exc
