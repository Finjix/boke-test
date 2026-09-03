"""Deterministic MiniMax H3 transformation prompts and content payloads."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from utils.errors import ValidationError


H3_PROMPT_VERSION = "h3-content-v7-seedream-storyboard"
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
    localization_prompt: str | None = None,
    segment_index: int = 1,
    total_segments: int | None = None,
    has_previous_generated_reference: bool = False,
    has_original_frame_references: bool = False,
    has_seedream_references: bool = False,
    reference_shot_map: Iterable[str] = (),
) -> str:
    """Build an H3 prompt, optionally rooted in the Doubao localization plan."""

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
    doubao_plan = localization_prompt.strip() if localization_prompt else ""
    if localization_prompt is not None and not doubao_plan:
        raise ValidationError("Doubao localization prompt cannot be empty")
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
    seedream_note = (
        "The supplied Seedream reference images are the target visual source for the listed "
        "storyboard shots. Match their localized people, wardrobe, environment, architecture, "
        "props, vehicles and visible text. Keep the source video as the authoritative source for "
        "motion, timing, composition and creative structure. Do not reuse the previous H3 segment "
        "or the original background."
        if has_seedream_references
        else ""
    )
    shot_map = [str(item).strip() for item in reference_shot_map if str(item).strip()]
    lines = [
        "Transform the supplied source video into a polished localized version.",
        f"Target dialogue language: {target_language}. Target region: {target_region}. "
        f"Target locale: {target_locale}.",
        f"This is {segment_label}. {instruction}.",
    ]
    if doubao_plan:
        lines.extend(
            [
                "The following Doubao Seed analysis is the authoritative localization plan. "
                "Apply its concrete shot-by-shot instructions to the source video; do not "
                "replace it with a generic dubbing or character-only interpretation.",
                "BEGIN DOUBAO LOCALIZATION PLAN",
                doubao_plan,
                "END DOUBAO LOCALIZATION PLAN",
            ]
        )
    lines.extend(
        [
            "The source video is the authoritative reference for the story, creative idea, "
            "character count and roles, shot order, camera movement, framing, composition, "
            "action timing, transitions, editing rhythm, pacing and overall effect only. "
            "It is not the target appearance or background reference to preserve.",
            "Maintain strict continuity of the same people's identity cues, face, age, hair, "
            "body proportions, silhouette, accessories, gestures, position, relationships and "
            "action beats. Preserve only the scene's spatial composition, object relationships, "
            "occlusion logic, lighting direction and camera geography; do not preserve the source "
            "location's visual identity or surface design.",
            "Mandatory full-scene transformation - PRIORITY 1: localize the same people AND rebuild "
            "the visible setting in every shot according to the Doubao plan. The target-region "
            f"environment must be the rendered base scene and must read as an authentic {target_region} "
            "location even in areas where no person is present. Redesign architecture, building "
            "facade, streetscape, storefronts, wall graphics, signs, vehicles, furniture, food, "
            "packaging, props and other culturally specific details. Preserve their screen position, "
            "scale, depth, occlusion and camera parallax only as structural relationships.",
            "Do not output a character-only edit, a dubbing-only edit, a recolor, or a foreground "
            "overlay on the original background. Do not preserve, copy, or leave recognizable any "
            "original building facade, business identity, storefront design, neon wall graphic, "
            "source-language sign or other source-specific environment detail unless the Doubao plan "
            "explicitly says that exact item is unchanged. When people and background instructions "
            "compete, complete the background replacement while retaining the original motion and "
            "composition.",
            "Translate or redraw existing visible text, signs, labels, business names, packages "
            "and wall graphics in the target language in the same screen position, perspective, "
            "visual hierarchy, style intent and appearance timing. This is a visible-text hard "
            "constraint. No readable source-language letters may remain on localized elements. "
            "Do not add subtitles, watermarks, logos or unrelated UI overlays that are not part "
            "of the source creative.",
            "Keep the transformed people, environment, props, visible text and lighting logic "
            "consistent from shot to shot. Preserve continuity of the transformed setting while "
            "retaining the source camera path, composition and edit rhythm.",
            "If Seedream reference images are supplied, use each image for the matching shot's "
            "localized appearance and environment. Do not treat them as a replacement source "
            "video and do not copy unrelated subjects, poses or compositions.",
            continuity,
            frame_note,
            seedream_note,
            "Render natural spoken audio in the target language when speech is present, using "
            "the Doubao speaker mapping and target voice/performance guidance. Keep each speaker "
            "stable, preserve dialogue timing and emotion, synchronize mouth movement, and "
            "preserve the source music, ambience and sound-design intent unless an equivalent "
            "target-region replacement is required. Return a video with audio.",
            "Before finalizing, inspect the background throughout the shot, including frames where "
            "people do not cover it. If the original facade, signage, wall graphics or source "
            "business identity is still recognizable, continue the scene redraw instead of "
            "returning a character-only result.",
            "Do not describe this instruction in the video; perform the transformation.",
        ]
    )
    if shot_map:
        lines.insert(
            8,
            "Storyboard reference map:\n" + "\n".join(f"- {item}" for item in shot_map),
        )
    prompt = "\n".join(lines)
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
