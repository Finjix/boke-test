from __future__ import annotations

import unittest

from core.localization import (
    localization_package_schema,
    validate_localization_package,
)
from core.models import JobSpec, LocalizationPackage, UploadedAsset
from core.seedance_prompt import build_seedance_content, build_seedance_prompt
from utils.errors import ValidationError


def _package(dialogues=None, *, target_language: str = "ar") -> dict:
    return {
        "source": {"language": "en"},
        "target": {"language": target_language, "region": "Gulf", "locale": "ar-SA"},
        "video_analysis": {
            "theme": "conversation",
            "story_structure": "setup and response",
            "shot_structure": "medium alternating shots",
            "scene_environment": "office",
            "character_relationships": "two colleagues",
            "product_information": "none",
            "core_creative": "preserve the exchange",
        },
        "speakers": [
            {"id": "speaker_1", "visual_hint": "left person"},
            {"id": "speaker_2", "visual_hint": "off-screen narrator"},
        ],
        "dialogues": dialogues
        if dialogues is not None
        else [
            {
                "speaker_id": "speaker_1",
                "start_ms": 520,
                "end_ms": 1800,
                "source_text": "Hello",
                "target_text": "مرحبا",
            },
            {
                "speaker_id": "speaker_2",
                "start_ms": 3100,
                "end_ms": 4450,
                "source_text": "Welcome",
                "target_text": "أهلا",
            },
        ],
        "visual_localization": {
            "characters": "modern Gulf business people",
            "wardrobe": "locally appropriate contemporary wardrobe",
            "environment": "Riyadh-style modern office",
            "architecture": "regional modern architecture",
            "props": "preserve product props",
        },
        "cultural_requirements": [
            "Keep the portrayal respectful and natural for the Gulf market"
        ],
    }


def _job() -> JobSpec:
    return JobSpec(
        input_video="input.mp4",
        target_language="ar",
        target_region="Gulf",
        target_locale="ar-SA",
    )


class LocalizationAndPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw_package = _package()
        self.package = LocalizationPackage.model_validate(self.raw_package)

    def test_schema_requires_localization_package_sections(self) -> None:
        schema = localization_package_schema()
        self.assertEqual(
            schema["required"],
            [
                "source",
                "target",
                "video_analysis",
                "speakers",
                "dialogues",
                "visual_localization",
                "cultural_requirements",
            ],
        )

    def test_valid_package_uses_integer_milliseconds_and_role_mapping(self) -> None:
        result = validate_localization_package(
            self.raw_package,
            target_language="ar",
            target_region="Gulf",
            target_locale="ar-SA",
            duration_seconds=6,
        )
        self.assertEqual(result.dialogues[0].start_ms, 520)
        self.assertEqual(result.dialogues[1].speaker_id, "speaker_2")
        self.assertEqual(result.visual_localization["environment"], "Riyadh-style modern office")

    def test_missing_role_is_rejected(self) -> None:
        invalid = _package(
            [
                {
                    "speaker_id": "speaker_missing",
                    "start_ms": 0,
                    "end_ms": 1000,
                    "source_text": "Hello",
                    "target_text": "مرحبا",
                }
            ]
        )
        with self.assertRaises(ValidationError):
            validate_localization_package(
                invalid,
                target_language="ar",
                target_region="Gulf",
                target_locale="ar-SA",
                duration_seconds=2,
            )

    def test_negative_or_non_integer_timestamps_are_rejected(self) -> None:
        negative = _package(
            [
                {
                    "speaker_id": "speaker_1",
                    "start_ms": -1,
                    "end_ms": 1000,
                    "source_text": "Hello",
                    "target_text": "مرحبا",
                }
            ]
        )
        non_integer = _package(
            [
                {
                    "speaker_id": "speaker_1",
                    "start_ms": 0.5,
                    "end_ms": 1000,
                    "source_text": "Hello",
                    "target_text": "مرحبا",
                }
            ]
        )
        for invalid in (negative, non_integer):
            with self.assertRaises(ValidationError):
                validate_localization_package(
                    invalid,
                    target_language="ar",
                    target_region="Gulf",
                    target_locale="ar-SA",
                    duration_seconds=2,
                )

    def test_timestamp_beyond_video_duration_is_rejected(self) -> None:
        invalid = _package(
            [
                {
                    "speaker_id": "speaker_1",
                    "start_ms": 1000,
                    "end_ms": 2501,
                    "source_text": "Hello",
                    "target_text": "مرحبا",
                }
            ]
        )
        with self.assertRaises(ValidationError):
            validate_localization_package(
                invalid,
                target_language="ar",
                target_region="Gulf",
                target_locale="ar-SA",
                duration_seconds=2,
            )

    def test_unsorted_duplicate_and_abnormally_overlapping_dialogue_are_rejected(self) -> None:
        unsorted = _package(
            [
                {
                    "speaker_id": "speaker_1",
                    "start_ms": 2000,
                    "end_ms": 3000,
                    "source_text": "Two",
                    "target_text": "اثنان",
                },
                {
                    "speaker_id": "speaker_2",
                    "start_ms": 0,
                    "end_ms": 1000,
                    "source_text": "One",
                    "target_text": "واحد",
                },
            ]
        )
        duplicate_line = {
            "speaker_id": "speaker_1",
            "start_ms": 0,
            "end_ms": 1000,
            "source_text": "Hello",
            "target_text": "مرحبا",
        }
        duplicate = _package([duplicate_line, dict(duplicate_line)])
        overlap = _package(
            [
                {
                    "speaker_id": "speaker_1",
                    "start_ms": 0,
                    "end_ms": 1000,
                    "source_text": "One",
                    "target_text": "واحد",
                },
                {
                    "speaker_id": "speaker_2",
                    "start_ms": 200,
                    "end_ms": 800,
                    "source_text": "Two",
                    "target_text": "اثنان",
                },
            ]
        )
        for invalid in (unsorted, duplicate, overlap):
            with self.assertRaises(ValidationError):
                validate_localization_package(
                    invalid,
                    target_language="ar",
                    target_region="Gulf",
                    target_locale="ar-SA",
                    duration_seconds=4,
                )

    def test_target_settings_mismatch_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            validate_localization_package(
                self.raw_package,
                target_language="ja",
                target_region="Japan",
                target_locale="ja-JP",
                duration_seconds=6,
            )

    def test_seedance_content_uses_video_and_refs_and_native_audio_generation_prompt(self) -> None:
        asset = UploadedAsset(
            local_path="ref.png",
            remote_url="https://uguu.se/ref.png",
            uploaded_at="now",
            kind="character_reference",
        )
        content = build_seedance_content(
            "https://uguu.se/input.mp4",
            self.package,
            [asset],
            _job(),
        )
        self.assertEqual(
            [item["type"] for item in content],
            ["text", "video_url", "image_url"],
        )
        prompt = str(content[0]["text"])
        self.assertIn("Gulf", prompt)
        self.assertIn("ar-SA", prompt)
        self.assertIn("background music", prompt)
        self.assertIn("Riyadh-style modern office", prompt)
        self.assertNotIn("audio_url", str(content))

    def test_seedance_prompt_rejects_package_job_mismatch(self) -> None:
        mismatched_job = JobSpec(
            input_video="input.mp4",
            target_language="ja",
            target_region="Japan",
            target_locale="ja-JP",
        )
        with self.assertRaises(ValidationError):
            build_seedance_prompt(self.package, mismatched_job)
