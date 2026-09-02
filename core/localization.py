"""Doubao video-understanding contract for the localization pipeline."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core.models import LocalizationDialogue, LocalizationPackage
from utils.errors import ValidationError
from utils.json_parser import parse_strict_json
from utils.logger import JobLogger


ANALYSIS_PROMPT_VERSION = "v3"
ANALYSIS_CORRECTION_ATTEMPTS = 2


def _https(value: str, label: str) -> str:
    if not value.startswith("https://"):
        raise ValidationError(f"{label} must be an HTTPS URL")
    return value


def localization_package_schema() -> dict[str, Any]:
    """Return the closed top-level JSON contract embedded in the prompt."""

    planning_object = {
        "type": "object",
        "additionalProperties": True,
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "source",
            "target",
            "video_analysis",
            "speakers",
            "dialogues",
            "visual_localization",
            "cultural_requirements",
        ],
        "properties": {
            "source": {
                "type": "object",
                "required": ["language"],
                "additionalProperties": True,
                "properties": {"language": {"type": "string"}},
            },
            "target": {
                "type": "object",
                "required": ["language", "region", "locale"],
                "additionalProperties": True,
                "properties": {
                    "language": {"type": "string"},
                    "region": {"type": "string"},
                    "locale": {"type": "string"},
                },
            },
            "video_analysis": planning_object,
            "speakers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "visual_hint"],
                    "properties": {
                        "id": {"type": "string"},
                        "visual_hint": {"type": "string"},
                    },
                },
            },
            "dialogues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "speaker_id",
                        "start_ms",
                        "end_ms",
                        "source_text",
                        "target_text",
                    ],
                    "properties": {
                        "speaker_id": {"type": "string"},
                        "start_ms": {"type": "integer", "minimum": 0},
                        "end_ms": {"type": "integer", "minimum": 0},
                        "source_text": {"type": "string"},
                        "target_text": {"type": "string"},
                    },
                },
            },
            "visual_localization": planning_object,
            "cultural_requirements": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
    }


def build_video_analysis_messages(
    source_video_url: str,
    *,
    target_language: str,
    target_region: str,
    target_locale: str,
    correction_error: str | None = None,
) -> list[dict[str, Any]]:
    """Build one multimodal Ark request for planning and translation."""

    source_video_url = _https(source_video_url, "source video")
    schema = json.dumps(
        localization_package_schema(),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    system = (
        "Analyze the complete video using both visual and audio information where available.\n"
        "Identify the theme, story structure, shot structure, scene environment, character "
        "relationships, product information and core creative idea.\n"
        "Identify every speaking character and maintain a stable speaker ID for the same "
        "character throughout the video.\n"
        "Determine which character speaks each dialogue line using visible speaking behavior "
        "and audio context.\n"
        "Transcribe the original dialogue and translate it directly into the requested target "
        "language while preserving meaning, tone, relationships and approximate timing.\n"
        "Create a practical visual localization plan for characters, wardrobe, environment, "
        "architecture and props, plus concrete cultural requirements for the target market.\n"
        "Return dialogue start and end timestamps as integer milliseconds.\n"
        "Do not describe voice timbre, music, ambience or sound effects; Seedance will generate "
        "the complete final audio scene.\n"
        "Use only the following JSON object shape and return valid JSON only:\n"
        f"{schema}"
    )
    user_text = (
        f"Create a localization package for region {target_region}, language code "
        f"{target_language}, locale {target_locale}. Detect the source language automatically. "
        "Keep dialogues sorted by start_ms. If a character is an off-screen narrator, use a "
        "stable speaker ID and visual_hint of 'off-screen narrator'."
    )
    if correction_error:
        user_text += (
            " The previous response failed local contract validation. Correct it and return "
            f"the complete JSON object again. Validation error: {correction_error}"
        )
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": [
                {"type": "video_url", "video_url": {"url": source_video_url}},
                {"type": "text", "text": user_text},
            ],
        },
    ]


def _overlap_is_abnormally_large(
    left: LocalizationDialogue,
    right: LocalizationDialogue,
) -> bool:
    overlap = min(left.end_ms, right.end_ms) - max(left.start_ms, right.start_ms)
    if overlap <= 0:
        return False
    shorter_duration = min(left.end_ms - left.start_ms, right.end_ms - right.start_ms)
    return overlap > shorter_duration * 0.5


def validate_localization_package(
    value: LocalizationPackage | dict[str, Any],
    *,
    target_language: str,
    target_region: str,
    target_locale: str,
    duration_seconds: float,
) -> LocalizationPackage:
    """Validate the model result against the job target and source duration."""

    try:
        package = (
            value
            if isinstance(value, LocalizationPackage)
            else LocalizationPackage.model_validate(value)
        )
    except Exception as exc:  # Pydantic's detailed error is not user-facing
        raise ValidationError(f"invalid localization package: {exc}") from exc

    if package.target_language.casefold() != target_language.casefold():
        raise ValidationError(
            "analysis target language does not match requested language: "
            f"{package.target_language} != {target_language}"
        )
    if package.target_region.casefold() != target_region.casefold():
        raise ValidationError(
            "analysis target region does not match requested region: "
            f"{package.target_region} != {target_region}"
        )
    if package.target_locale.casefold() != target_locale.casefold():
        raise ValidationError(
            "analysis target locale does not match requested locale: "
            f"{package.target_locale} != {target_locale}"
        )
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise ValidationError("source duration must be positive and finite")
    duration_ms = duration_seconds * 1000
    previous: LocalizationDialogue | None = None
    seen: set[tuple[str, int, int, str, str]] = set()
    for dialogue in package.dialogues:
        if dialogue.end_ms > duration_ms:
            raise ValidationError(
                f"dialogue {dialogue.speaker_id} ends after source video duration"
            )
        key = (
            dialogue.speaker_id,
            dialogue.start_ms,
            dialogue.end_ms,
            dialogue.source_text,
            dialogue.target_text,
        )
        if key in seen:
            raise ValidationError("localization package contains duplicate dialogue")
        seen.add(key)
        if previous is not None:
            if (dialogue.start_ms, dialogue.end_ms) < (previous.start_ms, previous.end_ms):
                raise ValidationError("dialogues must be sorted by start_ms")
            if _overlap_is_abnormally_large(previous, dialogue):
                raise ValidationError("dialogues contain an abnormally large overlap")
        previous = dialogue
    return package


def validate_localization_script(
    value: LocalizationPackage | dict[str, Any],
    *,
    target_language: str,
    duration_seconds: float,
) -> LocalizationPackage:
    """Compatibility wrapper for callers of the pre-package function name."""

    package = (
        value
        if isinstance(value, LocalizationPackage)
        else LocalizationPackage.model_validate(value)
    )
    return validate_localization_package(
        package,
        target_language=target_language,
        target_region=package.target_region,
        target_locale=package.target_locale,
        duration_seconds=duration_seconds,
    )


def analyze_video(
    client: Any,
    source_video_url: str,
    *,
    target_language: str,
    target_region: str,
    target_locale: str,
    duration_seconds: float,
    raw_dir: Path | None = None,
    logger: JobLogger | None = None,
    attempt_callback: Callable[[dict[str, Any]], None] | None = None,
) -> LocalizationPackage:
    """Call Doubao once, with one same-model correction attempt."""

    last_error: Exception | None = None
    correction_error: str | None = None
    for attempt in range(1, ANALYSIS_CORRECTION_ATTEMPTS + 1):
        attempt_started = datetime.now(timezone.utc).isoformat()
        messages = build_video_analysis_messages(
            source_video_url,
            target_language=target_language,
            target_region=target_region,
            target_locale=target_locale,
            correction_error=correction_error,
        )
        response: Any = None
        try:
            response = client.chat(
                messages,
                stage=f"video_analysis_attempt_{attempt}",
                raw_dir=raw_dir,
                response_format={"type": "json_object"},
            )
            content = client.extract_text(response)
            value = parse_strict_json(content, description="video localization package")
            package = validate_localization_package(
                value,
                target_language=target_language,
                target_region=target_region,
                target_locale=target_locale,
                duration_seconds=duration_seconds,
            )
        except ValidationError as exc:
            if attempt_callback is not None:
                attempt_callback(
                    {
                        "attempt": attempt,
                        "status": "failed",
                        "started_at": attempt_started,
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "request_id": getattr(response, "request_id", None),
                        "raw_response_path": (
                            str(response.raw_path)
                            if getattr(response, "raw_path", None)
                            else None
                        ),
                        "error": {"message": str(exc)},
                    }
                )
            last_error = exc
            correction_error = str(exc)
            if logger is not None:
                logger.warning(
                    "video localization package failed contract validation",
                    attempt=attempt,
                    error=str(exc),
                )
            if attempt == ANALYSIS_CORRECTION_ATTEMPTS:
                raise
        except Exception as exc:  # noqa: BLE001 - provider errors are recorded then re-raised
            if attempt_callback is not None:
                attempt_callback(
                    {
                        "attempt": attempt,
                        "status": "failed",
                        "started_at": attempt_started,
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "request_id": getattr(response, "request_id", None)
                        or getattr(exc, "request_id", None)
                        or getattr(client, "last_request_id", None),
                        "raw_response_path": (
                            str(response.raw_path)
                            if getattr(response, "raw_path", None)
                            else getattr(exc, "raw_response_path", None)
                        ),
                        "error": {
                            "message": str(exc),
                            "error_code": getattr(exc, "error_code", None),
                        },
                    }
                )
            raise
        else:
            if attempt_callback is not None:
                attempt_callback(
                    {
                        "attempt": attempt,
                        "status": "completed",
                        "started_at": attempt_started,
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "request_id": getattr(response, "request_id", None),
                        "raw_response_path": (
                            str(response.raw_path)
                            if getattr(response, "raw_path", None)
                            else None
                        ),
                    }
                )
            return package
    assert last_error is not None
    raise last_error
