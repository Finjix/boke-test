"""Doubao video-understanding contract for the localization pipeline."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core.models import LocalizationDialogue, LocalizationPackage
from language_config import canonical_language_code, language_values_match
from utils.errors import ValidationError
from utils.json_parser import parse_strict_json
from utils.logger import JobLogger


ANALYSIS_PROMPT_VERSION = "v7-seedream-storyboard-h3"
ANALYSIS_CORRECTION_ATTEMPTS = 2
DOUBAO_TIMELINE_CLAMP_TOLERANCE_MS = 1000


def _https(value: str, label: str) -> str:
    if not value.startswith("https://"):
        raise ValidationError(f"{label} must be an HTTPS URL")
    return value


def localization_package_schema(
    *,
    require_h3_prompt: bool = False,
    require_reference_plan: bool = False,
) -> dict[str, Any]:
    """Return the closed top-level JSON contract embedded in the prompt."""

    planning_object = {
        "type": "object",
        "additionalProperties": True,
    }
    required = [
        "source",
        "target",
        "video_analysis",
        "speakers",
        "dialogues",
        "visual_localization",
        "cultural_requirements",
    ]
    properties: dict[str, Any] = {
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
        "reference_shots": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "shot_id",
                    "start_ms",
                    "end_ms",
                    "keyframe_ms",
                    "character_ids",
                    "continuity_group",
                    "scene_description",
                    "replacement_requirements",
                    "preserve_requirements",
                    "seedream_prompt",
                ],
                "properties": {
                    "shot_id": {"type": "string", "minLength": 1},
                    "start_ms": {"type": "integer", "minimum": 0},
                    "end_ms": {"type": "integer", "minimum": 1},
                    "keyframe_ms": {"type": "integer", "minimum": 0},
                    "character_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "continuity_group": {"type": "string", "minLength": 1},
                    "scene_description": {"type": "string", "minLength": 1},
                    "replacement_requirements": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "preserve_requirements": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "seedream_prompt": {"type": "string", "minLength": 1},
                },
            },
        },
    }
    if require_h3_prompt:
        required.append("h3_prompt")
        properties["h3_prompt"] = {
            "type": "string",
            "minLength": 1,
            "maxLength": LocalizationPackage.MAX_H3_PROMPT_CHARS,
        }
    if require_reference_plan:
        required.append("reference_shots")
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


def build_video_analysis_messages(
    source_video_url: str,
    *,
    target_language: str,
    target_region: str,
    target_locale: str,
    correction_error: str | None = None,
    require_h3_prompt: bool = False,
    require_reference_plan: bool = False,
    transformation_instruction: str = "",
) -> list[dict[str, Any]]:
    """Build one multimodal Ark request for planning and translation."""

    source_video_url = _https(source_video_url, "source video")
    schema = json.dumps(
        localization_package_schema(
            require_h3_prompt=require_h3_prompt,
            require_reference_plan=require_reference_plan,
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    h3_instruction = (
        "Return h3_prompt as one concise, provider-ready H3 instruction in English. It must "
        "be concrete and shot-aware: describe the target-region transformation of every "
        "visible person, wardrobe, environment, architecture, streetscape, storefront, "
        "sign, vehicle, food, packaging, prop, lighting cue and other text; describe target "
        "speech, stable speaker voice/performance, emotion, speaker identity, dialogue timeline "
        "and mouth synchronization. "
        "Explicitly preserve the source character count and roles, relationships, actions, "
        "composition, camera path, shot order, transitions, edit rhythm, pacing and overall "
        "creative effect. Existing visible text must be translated or redrawn in place with "
        "the same timing and design intent; do not add subtitles, watermarks or unrelated UI."
        if require_h3_prompt
        else ""
    )
    reference_instruction = (
        "Return reference_shots as an ordered, complete storyboard. Create exactly one entry "
        "for every detected shot, including shots with no speaking person. Each entry must give "
        "the shot start/end in integer milliseconds, one safe keyframe timestamp inside the shot, "
        "stable visual character_ids and continuity_group values, a concrete scene description, explicit "
        "replacement_requirements, explicit preserve_requirements, and a concise Seedream 5.0 Pro "
        "image-edit prompt. The Seedream prompt must request a target-region re-render of the "
        "source keyframe that changes both people/wardrobe and the visible environment, architecture, "
        "vehicles, props, packaging and signs as required, while preserving character count, pose, "
        "relationships, composition, camera geography, lighting direction and object placement. "
        "The character_ids field is a visual-person namespace and may include non-speaking or "
        "background people; it is independent from the speaker_id namespace used for dialogue. "
        "Do not write a generic style prompt and do not omit a background-only shot."
        if require_reference_plan
        else ""
    )
    system = (
        "Analyze the complete video using both visual and audio information where available.\n"
        "Identify the theme, story structure, shot structure, scene environment, character "
        "relationships, product information and core creative idea.\n"
        "Identify every speaking character and maintain a stable speaker ID for the same "
        "character throughout the video.\n"
        "Keep speaker IDs and visual character IDs as separate namespaces: speaker IDs map "
        "audio and dialogue, while storyboard character_ids identify visible people and may "
        "include non-speaking or background people.\n"
        "Determine which character speaks each dialogue line using visible speaking behavior "
        "and audio context.\n"
        "Transcribe the original dialogue and translate it directly into the requested target "
        "language while preserving meaning, tone, relationships and approximate timing.\n"
        "Create a practical visual localization plan for characters, wardrobe, environment, "
        "architecture, visible text and props, plus concrete cultural requirements for the "
        "target market.\n"
        "Localize people and visible surroundings for the target region while preserving the "
        "same story, character roles, shot composition, camera movement, action timing and "
        "editing rhythm. Preserve layout and object relationships, but replace culturally "
        "specific appearance, setting, signage, packaging and other visible details where "
        "needed.\n"
        "Analyze target voice requirements for each speaker, including stable identity, "
        "delivery, emotion, timing and lip-sync intent, while retaining the source music and "
        "sound-design role unless localization requires an equivalent replacement.\n"
        "Return dialogue start and end timestamps as integer milliseconds.\n"
        f"{h3_instruction}\n"
        f"{reference_instruction}\n"
        "Use only the following JSON object shape and return valid JSON only:\n"
        f"{schema}"
    )
    user_text = (
        f"Create a localization package for region {target_region}, language code "
        f"{target_language}, locale {target_locale}. Detect the source language automatically. "
        "Keep dialogues sorted by start_ms. If a character is an off-screen narrator, use a "
        "stable speaker ID and visual_hint of 'off-screen narrator'."
    )
    if transformation_instruction.strip():
        user_text += (
            " Apply this additional user transformation requirement while preserving all "
            f"source invariants: {transformation_instruction.strip()}"
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
    transformation_instruction: str = "",
    require_h3_prompt: bool = False,
    require_reference_plan: bool = False,
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

    if not language_values_match(package.target_language, target_language):
        raise ValidationError(
            "analysis target language does not match requested language: "
            f"{package.target_language} != {target_language}"
        )
    # Persist one canonical code after accepting either the code or the
    # human-readable name. This keeps downstream provider prompts and the
    # legacy Seedance compatibility path deterministic.
    package.target["language"] = canonical_language_code(target_language)
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
    if require_h3_prompt and not package.h3_prompt:
        raise ValidationError("analysis localization package is missing h3_prompt")
    if require_h3_prompt:
        # Validate the complete text that will be sent to H3, including the
        # deterministic continuity instructions.  Use the largest reference
        # suffix so a later long-video segment cannot overflow the provider's
        # 7000-character limit after the package has been accepted.  Raising a
        # ValidationError here lets analyze_video ask Doubao for a shorter
        # correction; the pipeline never truncates the model output.
        from core.h3_prompt import build_transformation_prompt

        try:
            reference_shot_map = [
                f"image {index} = shot {shot.shot_id}, source time "
                f"{shot.start_ms}–{shot.end_ms} ms"
                for index, shot in enumerate(package.reference_shots, start=1)
            ]
            build_transformation_prompt(
                target_language=target_language,
                target_region=target_region,
                target_locale=target_locale,
                transformation_instruction=transformation_instruction,
                localization_prompt=package.h3_prompt,
                segment_index=1,
                has_seedream_references=True,
                reference_shot_map=reference_shot_map,
            )
        except ValidationError as exc:
            raise ValidationError(
                "Doubao h3_prompt would exceed H3's 7000-character limit; "
                f"return a shorter complete plan: {exc}"
            ) from exc
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise ValidationError("source duration must be positive and finite")
    duration_ms = duration_seconds * 1000
    if require_reference_plan:
        if not package.reference_shots:
            raise ValidationError("analysis localization package is missing reference_shots")
        for shot in package.reference_shots:
            if shot.end_ms > duration_ms:
                raise ValidationError(
                    f"reference shot {shot.shot_id} ends after source video duration"
                )
            # Visual storyboard IDs and dialogue speaker IDs intentionally use
            # separate namespaces. A non-speaking passerby or a person whose
            # voice is not present still needs a stable visual ID for Seedream.
            if len(shot.character_ids) != len(set(shot.character_ids)):
                raise ValidationError(
                    f"reference shot {shot.shot_id} contains duplicate visual character IDs"
                )
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


def recover_doubao_schema_wrapper(content: str) -> dict[str, Any] | None:
    """Recover the two known Doubao schema-wrapper response shapes.

    Some Ark responses return the requested package under a top-level
    JSON-schema-shaped wrapper. The wrapper may be a complete JSON value, or
    it may be followed by the known unrelated ``audio_path`` field. This
    helper stays intentionally narrow: only the expected v6/v7 package field
    sets and exact wrapper metadata are accepted. The returned candidate is
    validated by the caller with the normal localization-package contract
    before it is persisted.
    """

    text = content.strip()
    if not text.startswith("{"):
        return None
    try:
        wrapper, end = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(wrapper, dict):
        return None
    base_fields = {
        "source",
        "target",
        "video_analysis",
        "speakers",
        "dialogues",
        "visual_localization",
        "cultural_requirements",
        "h3_prompt",
    }
    allowed_field_sets = (base_fields, base_fields | {"reference_shots"})
    wrapper_keys = set(wrapper)
    if wrapper_keys not in (
        {"type", "additionalProperties", "required", "properties"},
        {"type", "additionalProperties", "required", "properties", "h3_prompt"},
    ):
        return None
    if wrapper.get("type") != "object" or wrapper.get("additionalProperties") is not False:
        return None
    required = wrapper.get("required")
    if (
        not isinstance(required, list)
        or not all(isinstance(field, str) for field in required)
        or len(required) != len(set(required))
        or set(required) not in allowed_field_sets
    ):
        return None
    required_fields = set(required)
    properties = wrapper.get("properties")
    if not isinstance(properties, dict):
        return None
    property_keys = set(properties)
    if property_keys not in (required_fields, required_fields - {"h3_prompt"}):
        return None
    top_level_h3_prompt = wrapper.get("h3_prompt")
    property_h3_prompt = properties.get("h3_prompt")
    if top_level_h3_prompt is not None and property_h3_prompt is not None:
        if top_level_h3_prompt != property_h3_prompt:
            return None
    h3_prompt = (
        top_level_h3_prompt
        if top_level_h3_prompt is not None
        else property_h3_prompt
    )
    if not isinstance(h3_prompt, str) or not h3_prompt.strip():
        return None

    suffix = text[end:].strip()
    if suffix:
        # The observed malformed content has one extra closing brace after
        # the unrelated field: `,<audio_path field>}}`. Strip only that
        # known outer brace before parsing the trailing field.
        if not suffix.startswith(",") or not suffix.endswith("}}"):
            return None
        try:
            extra = parse_strict_json(
                "{" + suffix[1:-1],
                description="Doubao schema-wrapper trailing field",
            )
        except Exception:
            return None
        if (
            not isinstance(extra, dict)
            or set(extra) != {"audio_path"}
            or not isinstance(extra.get("audio_path"), str)
        ):
            return None

    candidate = dict(properties)
    if isinstance(candidate.get("video_analysis"), str):
        candidate["video_analysis"] = {"summary": candidate["video_analysis"]}
    candidate["h3_prompt"] = h3_prompt.strip()
    return candidate


def _normalize_doubao_package(value: Any, *, duration_seconds: float) -> Any:
    """Normalize narrowly tolerated Doubao field and boundary variations."""

    if not isinstance(value, dict):
        return value
    normalized = dict(value)
    changed = False
    if isinstance(value.get("video_analysis"), str):
        normalized["video_analysis"] = {"summary": value["video_analysis"]}
        changed = True

    if math.isfinite(duration_seconds) and duration_seconds > 0:
        duration_ms = math.floor(duration_seconds * 1000)
        if duration_ms > 0:
            for field_name in ("dialogues", "reference_shots"):
                entries = value.get(field_name)
                if not isinstance(entries, list):
                    continue
                normalized_entries = list(entries)
                entries_changed = False
                for index, entry in enumerate(entries):
                    if not isinstance(entry, dict):
                        continue
                    start_ms = entry.get("start_ms")
                    end_ms = entry.get("end_ms")
                    if (
                        not isinstance(start_ms, int)
                        or isinstance(start_ms, bool)
                        or not isinstance(end_ms, int)
                        or isinstance(end_ms, bool)
                        or end_ms <= duration_ms
                        or end_ms - duration_ms > DOUBAO_TIMELINE_CLAMP_TOLERANCE_MS
                        or start_ms >= duration_ms
                    ):
                        continue
                    updated_entry = dict(entry)
                    updated_entry["end_ms"] = duration_ms
                    keyframe_ms = updated_entry.get("keyframe_ms")
                    if (
                        field_name == "reference_shots"
                        and isinstance(keyframe_ms, int)
                        and not isinstance(keyframe_ms, bool)
                        and keyframe_ms >= duration_ms
                        and start_ms < duration_ms - 1
                    ):
                        updated_entry["keyframe_ms"] = duration_ms - 1
                    normalized_entries[index] = updated_entry
                    entries_changed = True
                if entries_changed:
                    normalized[field_name] = normalized_entries
                    changed = True
    if not changed:
        return value
    return normalized


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
    transformation_instruction: str = "",
    require_h3_prompt: bool = False,
    require_reference_plan: bool = False,
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
            require_h3_prompt=require_h3_prompt,
            require_reference_plan=require_reference_plan,
            transformation_instruction=transformation_instruction,
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
            value = recover_doubao_schema_wrapper(content)
            if value is None:
                value = parse_strict_json(
                    content,
                    description="video localization package",
                    allow_trailing_explanation=True,
                )
            value = _normalize_doubao_package(value, duration_seconds=duration_seconds)
            package = validate_localization_package(
                value,
                target_language=target_language,
                target_region=target_region,
                target_locale=target_locale,
                duration_seconds=duration_seconds,
                transformation_instruction=transformation_instruction,
                require_h3_prompt=require_h3_prompt,
                require_reference_plan=require_reference_plan,
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
