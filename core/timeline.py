"""ASR normalization and translation timeline validation."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any

from core.models import Segment
from utils.errors import ValidationError


def normalize_asr(raw: dict[str, Any]) -> list[Segment]:
    result = raw.get("result") if isinstance(raw, dict) else None
    subtitles = result.get("subtitles") if isinstance(result, dict) else None
    if not isinstance(subtitles, list):
        raise ValidationError("MediaKit ASR response has no result.subtitles list")

    segments: list[Segment] = []
    for index, item in enumerate(subtitles, start=1):
        if not isinstance(item, dict):
            raise ValidationError(f"ASR subtitle {index} is not an object")
        text = str(item.get("subtitle_text", "")).strip()
        if not text:
            continue
        try:
            segment = Segment(
                id=f"seg_{len(segments) + 1:04d}",
                speaker=str(item.get("speaker", "")).strip(),
                start=float(item["start_time"]),
                end=float(item["end_time"]),
                text=text,
                confidence=(
                    float(item["confidence"])
                    if item.get("confidence") is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError(f"Invalid ASR subtitle {index}: {exc}") from exc
        segments.append(segment)

    if not segments:
        raise ValidationError("ASR produced no non-empty dialogue segments")
    return segments


def validate_segments(segments: Sequence[Segment]) -> None:
    if not segments:
        raise ValidationError("timeline cannot be empty")
    ids: set[str] = set()
    for segment in segments:
        if segment.id in ids:
            raise ValidationError(f"duplicate segment id: {segment.id}")
        ids.add(segment.id)
        if not math.isfinite(segment.start) or not math.isfinite(segment.end):
            raise ValidationError(f"non-finite timestamps in {segment.id}")


def _coerce_translated(value: Any) -> list[Segment]:
    if not isinstance(value, list):
        raise ValidationError("translation response must be a JSON array")
    try:
        return [Segment.model_validate(item) for item in value]
    except Exception as exc:  # Pydantic's detailed error is not user-facing
        raise ValidationError(f"invalid translated timeline: {exc}") from exc


def validate_translation(
    source: Sequence[Segment],
    translated_value: Any,
) -> list[Segment]:
    validate_segments(source)
    translated = _coerce_translated(translated_value)
    if len(source) != len(translated):
        raise ValidationError(
            f"translation segment count changed: {len(source)} -> {len(translated)}"
        )

    for original, localized in zip(source, translated, strict=True):
        if original.id != localized.id:
            raise ValidationError(f"translation changed segment id: {original.id}")
        if original.speaker != localized.speaker:
            raise ValidationError(f"translation changed speaker: {original.id}")
        if not math.isclose(original.start, localized.start, abs_tol=1e-6):
            raise ValidationError(f"translation changed start timestamp: {original.id}")
        if not math.isclose(original.end, localized.end, abs_tol=1e-6):
            raise ValidationError(f"translation changed end timestamp: {original.id}")
        if not localized.text.strip():
            raise ValidationError(f"translation text is empty: {original.id}")
    return translated


def segments_as_dicts(segments: Iterable[Segment]) -> list[dict[str, Any]]:
    return [segment.model_dump(mode="json") for segment in segments]


def speaker_ids(segments: Sequence[Segment]) -> list[str]:
    return list(dict.fromkeys(segment.speaker for segment in segments))
