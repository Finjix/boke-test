"""Deterministic MiniMax H3 transformation prompts and content payloads."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from utils.errors import ValidationError


H3_PROMPT_VERSION = "h3-content-v2"
H3_MAX_PROMPT_CHARS = 7000
H3_MAX_IMAGES = 9
H3_MAX_FILES = 12


def _default_transformation_instruction(target_region: str) -> str:
    return (
        f"Perform a full visual localization for {target_region}. This is not a dubbing-only "
        "or character-only conversion: make the complete visible environment feel native to "
        "the target region while preserving the source story and motion."
    )


def _https(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("https://"):
        raise ValidationError(f"{label} must be an HTTPS URL")
    return value


def _asset_url(asset: Any, label: str) -> str:
    if isinstance(asset, (str, Path)):
        return _https(str(asset), label)
    value = getattr(asset, "remote_url", None)
    return _https(str(value), label)


def build_transformation_prompt(
    *,
    target_language: str,
    target_region: str,
    target_locale: str,
    transformation_instruction: str = "",
    segment_index: int = 1,
    total_segments: int | None = None,
    has_previous_generated_reference: bool = False,
    has_original_frame_references: bool = False,
) -> str:
    """Build the provider prompt without an intermediate LLM call."""

    if not target_language.strip() or not target_region.strip() or not target_locale.strip():
        raise ValidationError("H3 target language, region and locale are required")
    if segment_index <= 0:
        raise ValidationError("H3 segment index must be positive")
    segment_label = (
        f"segment {segment_index} of {total_segments}"
        if total_segments
        else f"segment {segment_index}"
    )
    instruction = transformation_instruction.strip() or _default_transformation_instruction(
        target_region
    )
    continuity = (
        "A previous generated segment is supplied only as a continuity reference. Use it "
        "to keep the same people, scene design, wardrobe language and visual style; the "
        "current source video remains the authoritative creative and motion reference."
        if has_previous_generated_reference
        else ""
    )
    frame_note = (
        "Uniform frames from the original master are supplied as additional identity and "
        "scene references; do not turn them into a slideshow."
        if has_original_frame_references
        else ""
    )
    prompt = "\n".join(
        [
            "Transform the supplied source video into a polished localized version.",
            f"Target dialogue language: {target_language}. Target region: {target_region}. "
            f"Target locale: {target_locale}.",
            f"This is {segment_label}. {instruction}.",
            "The current source video is the core reference for content, motion and timing, "
            "not an instruction to copy its unlocalized appearance. Preserve the original "
            "creative idea, story meaning, shot order, camera movement, framing, composition, "
            "action timing, transitions, editing rhythm, pacing and overall visual effect.",
            "Maintain strict continuity of each person's identity, face, age, hair, body "
            "proportions, clothing silhouette, accessories, gestures and position. Maintain "
            "the layout of the scene, important props, lighting logic, geography and object "
            "relationships. Do not add or remove story beats merely because the target region "
            "is different.",
            "Mandatory full-scene transformation: change the people AND the visible setting in "
            "every shot. Treat the background as a first-class output. Where visible, redesign "
            "the architecture and streetscape, storefronts and signage, vehicles, furniture, "
            "food or packaging, props, wardrobe and other culturally specific details so the "
            f"scene reads as an authentic {target_region} version. Do not leave the original "
            "location, building facade, business signs, vehicles or background unchanged, and "
            "do not merely recolor the source or dub its audio. Keep the same characters, scene "
            "roles, relative placement and action beats across the entire segment; do not invent "
            "a different plot or unrelated replacement characters.",
            "Keep the transformed people, environment, props and lighting logic consistent from "
            "shot to shot. Preserve continuity of the transformed setting while retaining the "
            "source camera path, composition and edit rhythm.",
            "If reference images are supplied, use them as style, appearance or cultural "
            "guidance only. Do not treat them as a replacement source video and do not copy "
            "unrelated subjects, poses or compositions.",
            continuity,
            frame_note,
            "Render natural spoken audio in the target language when speech is present, with "
            "accurate speaker assignment, believable timing and synchronized mouth movement. "
            "Preserve non-speech audio intent and the source's sound design unless localization "
            "requires a culturally appropriate replacement. Return a video with audio.",
            "Do not show captions, watermarks, logos or UI overlays unless they are part of the "
            "source creative and must be preserved. Do not describe this instruction in the "
            "video; perform the transformation.",
        ]
    )
    prompt = "\n".join(line for line in prompt.splitlines() if line.strip())
    if len(prompt) > H3_MAX_PROMPT_CHARS:
        raise ValidationError("H3 prompt exceeds the 7000-character limit")
    return prompt


def build_h3_content(
    source_video_url: str,
    prompt: str,
    *,
    previous_video_url: str | None = None,
    reference_assets: Iterable[Any] = (),
    original_frame_assets: Iterable[Any] = (),
    continuity_image_url: str | None = None,
) -> list[dict[str, Any]]:
    """Build H3 full-reference content for one source slice.

    The source slice is always the first reference video. A previous generated
    video is optional and is never allowed to replace the current source. The
    old ``continuity_image_url`` argument is accepted for integrations that used
    an earlier prototype, but it is encoded as a normal reference image.
    """

    if not isinstance(prompt, str) or not prompt.strip():
        raise ValidationError("H3 prompt cannot be empty")
    if len(prompt) > H3_MAX_PROMPT_CHARS:
        raise ValidationError("H3 prompt exceeds the 7000-character limit")

    content: list[dict[str, Any]] = [
        {"type": "text", "text": prompt.strip()},
        {
            "type": "video_url",
            "video_url": {"url": _https(source_video_url, "H3 source video")},
            "role": "reference_video",
        },
    ]
    if previous_video_url:
        content.append(
            {
                "type": "video_url",
                "video_url": {"url": _https(previous_video_url, "previous H3 video")},
                "role": "reference_video",
            }
        )

    image_values = list(reference_assets) + list(original_frame_assets)
    if continuity_image_url:
        image_values.append(continuity_image_url)
    if len(image_values) > H3_MAX_IMAGES:
        raise ValidationError("H3 reference images cannot exceed 9 files")
    for index, asset in enumerate(image_values, start=1):
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": _asset_url(asset, f"H3 reference image {index}")},
                "role": "reference_image",
            }
        )
    if len(content) - 1 > H3_MAX_FILES:
        raise ValidationError("H3 multimodal reference inputs cannot exceed 12 files")
    return content
