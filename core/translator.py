"""Whole-timeline translation through the configured Doubao model."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from core.models import Segment
from core.timeline import segments_as_dicts, validate_translation
from utils.errors import ValidationError
from utils.json_parser import parse_strict_json
from utils.logger import JobLogger


def build_translation_messages(
    segments: Sequence[Segment],
    *,
    target_language: str,
    target_region: str,
) -> list[dict[str, str]]:
    serialized = json.dumps(segments_as_dicts(segments), ensure_ascii=False)
    return [
        {
            "role": "system",
            "content": (
                "你是视频本地化翻译器。保持 segment id、speaker、时间戳不变，"
                "只翻译 text。输出纯 JSON，不输出 Markdown。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"目标语言：{target_language}（{target_region}）。"
                "要求自然口语化、符合当地表达、保持角色语气、不增加新内容，"
                f"并尽可能控制译文长度以适配原始时间段。输入：{serialized}"
            ),
        },
    ]


def translate_segments(
    client: Any,
    segments: Sequence[Segment],
    *,
    target_language: str,
    target_region: str,
    raw_dir: Path | None = None,
    validation_attempts: int = 3,
    logger: JobLogger | None = None,
) -> list[Segment]:
    messages = build_translation_messages(
        segments,
        target_language=target_language,
        target_region=target_region,
    )
    last_error: Exception | None = None
    for attempt in range(1, validation_attempts + 1):
        try:
            response = client.chat(
                messages,
                stage=f"translation_attempt_{attempt}",
                raw_dir=raw_dir,
            )
            content = client.extract_text(response)
            translated_value = parse_strict_json(content, description="translation response")
            return validate_translation(segments, translated_value)
        except ValidationError as exc:
            last_error = exc
            if logger is not None:
                logger.warning(
                    "translation response failed contract validation",
                    attempt=attempt,
                    error=str(exc),
                )
            if attempt == validation_attempts:
                raise
    assert last_error is not None
    raise last_error
