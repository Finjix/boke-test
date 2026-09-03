"""Dynamic Seedance prompt construction for the localization pipeline."""

from __future__ import annotations

import json
from collections.abc import Iterable

from core.models import JobSpec, LocalizationPackage, UploadedAsset
from language_config import language_values_match
from utils.errors import ValidationError


SEEDANCE_PROMPT_VERSION = "v3"


def _https(value: str, label: str) -> str:
    if not value.startswith("https://"):
        raise ValidationError(f"{label} must be an HTTPS URL")
    return value


def _package_prompt_data(package: LocalizationPackage) -> dict[str, object]:
    return {
        "source": package.source,
        "target": package.target,
        "video_analysis": package.video_analysis,
        "speakers": [speaker.model_dump(mode="json") for speaker in package.speakers],
        "dialogues": [dialogue.model_dump(mode="json") for dialogue in package.dialogues],
        "visual_localization": package.visual_localization,
        "cultural_requirements": package.cultural_requirements,
    }


def build_seedance_prompt(package: LocalizationPackage, job: JobSpec) -> str:
    """Render the model-generated localization plan into one Seedance prompt."""

    if not language_values_match(package.target_language, job.target_language):
        raise ValidationError("localization package language does not match the job")
    if package.target_region.casefold() != job.target_region.casefold():
        raise ValidationError("localization package region does not match the job")
    if package.target_locale.casefold() != job.target_locale.casefold():
        raise ValidationError("localization package locale does not match the job")

    package_json = json.dumps(
        _package_prompt_data(package),
        ensure_ascii=False,
        indent=2,
    )
    return (
        "Create a localized version of reference video 1.\n"
        "Preserve the original story, creative concept, shot composition, camera movement, "
        "main actions, character relationships, shot order and pacing.\n"
        f"Target market: {job.target_region} ({job.target_locale}).\n"
        "Apply the generated character, wardrobe, environment, architecture, prop and cultural "
        "localization plan below while keeping the original creative structure recognizable.\n"
        "Generate a complete synchronized audio track inside the output video: natural speech "
        "in the target language, stable speaker identity for every speaker, matching dialogue "
        "timing and emotion, plus coherent background music, ambience and important sound effects.\n"
        "Synchronize mouth movement and performance to the generated target-language speech.\n"
        "Do not add dialogue, swap speakers, change the number of characters, add subtitles, "
        "or require any post-processing after video generation.\n"
        "Localization Package:\n"
        f"{package_json}"
    )


def build_seedance_content(
    source_url: str,
    package: LocalizationPackage,
    reference_assets: Iterable[UploadedAsset],
    job: JobSpec,
) -> list[dict[str, object]]:
    """Build Seedance text/video/image content with native audio generation."""

    content: list[dict[str, object]] = [
        {
            "type": "text",
            "text": build_seedance_prompt(package, job),
        },
        {
            "type": "video_url",
            "video_url": {"url": _https(source_url, "source video")},
            "role": "reference_video",
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
