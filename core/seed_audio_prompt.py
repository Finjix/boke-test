"""Prompt construction for Seed Audio 1.0 full-scene generation."""

from __future__ import annotations

import json

from core.models import LocalizationScript
from utils.errors import ValidationError


SEED_AUDIO_PROMPT_VERSION = "v2"


def _timestamp(milliseconds: int) -> str:
    minutes, remainder = divmod(milliseconds, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{minutes:02d}:{seconds:02d}.{millis:03d}"


def build_seed_audio_prompt(
    script: LocalizationScript,
    *,
    target_language: str,
    target_locale: str,
) -> str:
    """Build one prompt for the complete target-language sound scene."""

    if not target_language.strip() or not target_locale.strip():
        raise ValidationError("target language and locale are required for Seed Audio")

    lines = [
        "Use @Audio1 as the reference for the complete original audio scene.",
        "Generate a localized version in the requested target language and locale.",
        "Preserve the identity and relationship of every original speaker.",
        "Each translated dialogue line must be spoken by its corresponding speaker from the reference audio.",
        "Preserve the original performance style, emotion, conversational rhythm and approximate dialogue timing.",
        "Recreate the surrounding sound scene based on the reference audio, including background music, ambience and important sound effects.",
        "Do not add new dialogue. Do not swap speakers. Do not translate non-speech sound events.",
        f"Target language: {target_language}",
        f"Target locale: {target_locale}",
        "",
        "Target dialogue timeline:",
    ]
    if script.dialogues:
        for dialogue in script.dialogues:
            target_text = json.dumps(dialogue.target_text, ensure_ascii=False)
            lines.append(
                f"[{_timestamp(dialogue.start_ms)} - {_timestamp(dialogue.end_ms)}] "
                f"{dialogue.speaker_id}: {target_text}"
            )
    else:
        lines.append(
            "There are no dialogue lines. Recreate the complete non-speech sound scene "
            "from the reference audio without adding speech."
        )
    lines.extend(
        [
            "",
            "The reference audio is the complete original track with all original sound layers.",
            "Return one complete localized audio track suitable for direct use as the final audio condition.",
        ]
    )
    return "\n".join(lines)
