from __future__ import annotations

import unittest

from core.localization import validate_localization_script
from core.models import JobSpec, LocalizationScript, UploadedAsset
from core.seed_audio_prompt import build_seed_audio_prompt
from core.seedance_prompt import build_seedance_content
from utils.errors import ValidationError


def _script(dialogues=None, *, target_language: str = "ar") -> dict:
    return {
        "source_language": "en",
        "target_language": target_language,
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
    }


class LocalizationAndPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw_script = _script()
        self.script = LocalizationScript.model_validate(self.raw_script)

    def test_valid_script_uses_integer_milliseconds_and_role_mapping(self) -> None:
        result = validate_localization_script(
            self.raw_script,
            target_language="ar",
            duration_seconds=6,
        )
        self.assertEqual(result.dialogues[0].start_ms, 520)
        self.assertEqual(result.dialogues[1].speaker_id, "speaker_2")

    def test_missing_role_is_rejected(self) -> None:
        invalid = _script(
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
            validate_localization_script(invalid, target_language="ar", duration_seconds=2)

    def test_negative_or_non_integer_timestamps_are_rejected(self) -> None:
        negative = _script(
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
        non_integer = _script(
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
                validate_localization_script(invalid, target_language="ar", duration_seconds=2)

    def test_timestamp_beyond_video_duration_is_rejected(self) -> None:
        invalid = _script(
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
            validate_localization_script(invalid, target_language="ar", duration_seconds=2)

    def test_unsorted_duplicate_and_abnormally_overlapping_dialogue_are_rejected(self) -> None:
        unsorted = _script(
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
        duplicate = _script([duplicate_line, dict(duplicate_line)])
        overlap = _script(
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
                validate_localization_script(invalid, target_language="ar", duration_seconds=4)

    def test_target_language_mismatch_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            validate_localization_script(
                self.raw_script,
                target_language="ja",
                duration_seconds=6,
            )

    def test_seed_audio_prompt_requests_complete_scene_audio(self) -> None:
        prompt = build_seed_audio_prompt(
            self.script,
            target_language="ar",
            target_locale="ar-SA",
        )
        self.assertIn("@Audio1", prompt)
        self.assertIn("background music", prompt)
        self.assertIn("speaker_1", prompt)
        self.assertNotIn("DRY DIALOGUE ONLY", prompt)

    def test_seedance_content_contains_localized_audio_and_refs(self) -> None:
        job = JobSpec(
            input_video="input.mp4",
            target_language="ar",
            target_region="Gulf",
            target_locale="ar-SA",
        )
        asset = UploadedAsset(
            local_path="ref.png",
            remote_url="https://uguu.se/ref.png",
            uploaded_at="now",
            kind="character_reference",
        )
        content = build_seedance_content(
            "https://uguu.se/input.mp4",
            "https://uguu.se/localized_audio.wav",
            [asset],
            job,
        )
        self.assertEqual(
            [item["type"] for item in content],
            ["text", "video_url", "audio_url", "image_url"],
        )
        self.assertIn("ar-SA", content[0]["text"])
        self.assertEqual(content[2]["role"], "reference_audio")
        self.assertEqual(content[-1]["role"], "reference_image")

