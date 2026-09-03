from __future__ import annotations

import json
import unittest

from core.localization import (
    analyze_video,
    build_video_analysis_messages,
    localization_package_schema,
    recover_doubao_schema_wrapper,
    soften_provider_sensitive_language,
    validate_localization_package,
)
from core.models import JobSpec, LocalizationPackage, UploadedAsset
from core.seedance_prompt import build_seedance_content, build_seedance_prompt
from api.ark import extract_text
from api.common import ApiResponse
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

    def test_video_analysis_messages_accept_ark_base64_video_input(self) -> None:
        messages = build_video_analysis_messages(
            "data:video/mp4;base64,AAAA",
            target_language="ar",
            target_region="Saudi Arabia",
            target_locale="ar-SA",
        )
        video = messages[1]["content"][0]
        self.assertEqual(video["type"], "video_url")
        self.assertTrue(video["video_url"]["url"].startswith("data:video/mp4;base64,"))
        with self.assertRaises(ValidationError):
            build_video_analysis_messages(
                "file:///input.mp4",
                target_language="ar",
                target_region="Saudi Arabia",
                target_locale="ar-SA",
            )

    def test_active_schema_requires_h3_prompt(self) -> None:
        schema = localization_package_schema(require_h3_prompt=True)
        self.assertIn("h3_prompt", schema["required"])
        self.assertEqual(schema["properties"]["h3_prompt"]["maxLength"], 7000)
        with self.assertRaises(ValidationError):
            validate_localization_package(
                self.raw_package,
                target_language="ar",
                target_region="Gulf",
                target_locale="ar-SA",
                duration_seconds=6,
                require_h3_prompt=True,
            )

    def test_active_schema_requires_complete_seedream_storyboard(self) -> None:
        schema = localization_package_schema(
            require_h3_prompt=True,
            require_reference_plan=True,
        )
        self.assertIn("reference_shots", schema["required"])
        invalid = dict(self.raw_package)
        invalid["h3_prompt"] = "Short plan"
        with self.assertRaises(ValidationError):
            validate_localization_package(
                invalid,
                target_language="ar",
                target_region="Gulf",
                target_locale="ar-SA",
                duration_seconds=6,
                require_h3_prompt=True,
                require_reference_plan=True,
            )
        valid = dict(invalid)
        valid["reference_shots"] = [
            {
                "shot_id": "shot_001",
                "start_ms": 0,
                "end_ms": 6000,
                "keyframe_ms": 1000,
                "character_ids": ["speaker_1"],
                "continuity_group": "main",
                "scene_description": "A complete localized street scene",
                "replacement_requirements": ["replace the facade and signs"],
                "preserve_requirements": ["preserve composition and action"],
                "seedream_prompt": "Edit the keyframe into the target-region scene",
            }
        ]
        package = validate_localization_package(
            valid,
            target_language="ar",
            target_region="Gulf",
            target_locale="ar-SA",
            duration_seconds=6,
            require_h3_prompt=True,
            require_reference_plan=True,
        )
        self.assertEqual(package.reference_shots[0].keyframe_ms, 1000)

    def test_visual_character_ids_are_independent_from_speaker_ids(self) -> None:
        value = dict(self.raw_package)
        value["h3_prompt"] = "Short plan"
        value["reference_shots"] = [
            {
                "shot_id": "shot_001",
                "start_ms": 0,
                "end_ms": 6000,
                "keyframe_ms": 1000,
                "character_ids": [
                    "char_hooded_woman",
                    "char_suited_man",
                    "char_red_jacket_bystander",
                ],
                "continuity_group": "street-scene",
                "scene_description": "A complete localized street scene",
                "replacement_requirements": ["replace the facade and signs"],
                "preserve_requirements": ["preserve composition and action"],
                "seedream_prompt": "Edit the keyframe into the target-region scene",
            }
        ]
        package = validate_localization_package(
            value,
            target_language="ar",
            target_region="Gulf",
            target_locale="ar-SA",
            duration_seconds=6,
            require_h3_prompt=True,
            require_reference_plan=True,
        )
        self.assertEqual(
            package.reference_shots[0].character_ids,
            [
                "char_hooded_woman",
                "char_suited_man",
                "char_red_jacket_bystander",
            ],
        )

    def test_h3_prompt_overflow_triggers_same_model_correction_without_truncation(self) -> None:
        class FakeAnalysisClient:
            def __init__(self) -> None:
                self.calls = 0

            @staticmethod
            def extract_text(response: ApiResponse) -> str:
                return extract_text(response)

            def chat(self, messages, **kwargs):
                self.calls += 1
                payload = _package()
                payload["h3_prompt"] = "x" * 7000 if self.calls == 1 else "Short complete plan."
                return ApiResponse(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(payload, ensure_ascii=False)
                                }
                            }
                        ]
                    },
                    f"request-{self.calls}",
                )

        client = FakeAnalysisClient()
        details: list[dict] = []
        package = analyze_video(
            client,
            "https://example.test/source.mp4",
            target_language="ar",
            target_region="Gulf",
            target_locale="ar-SA",
            duration_seconds=6,
            require_h3_prompt=True,
            attempt_callback=details.append,
        )
        self.assertEqual(client.calls, 2)
        self.assertEqual(package.h3_prompt, "Short complete plan.")
        self.assertEqual([item["status"] for item in details], ["failed", "completed"])
        self.assertIn("7000-character limit", details[0]["error"]["message"])

    def test_provider_sensitive_prompt_language_is_softened_without_removing_action(self) -> None:
        softened = soften_provider_sensitive_language(
            "The thief is stealing the stolen wallet; this is a pickpocket theft."
            " 小偷正在偷走钱包，属于盗窃行为。"
        )
        self.assertEqual(
            softened,
            "The person is taking the taken wallet; this is a person reaching for a wallet "
            "wallet-taking moment. 人物正在拿走钱包，属于拿取动作。",
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

    def test_language_name_and_code_are_mutually_accepted_and_normalized(self) -> None:
        named = _package(target_language="Arabic")
        result = validate_localization_package(
            named,
            target_language="ar",
            target_region="Gulf",
            target_locale="ar-SA",
            duration_seconds=6,
        )
        self.assertEqual(result.target_language, "ar")
        self.assertEqual(result.target["language"], "ar")

        coded = _package(target_language="AR")
        result = validate_localization_package(
            coded,
            target_language="Arabic",
            target_region="Gulf",
            target_locale="ar-SA",
            duration_seconds=6,
        )
        self.assertEqual(result.target_language, "ar")

    def test_recover_doubao_schema_wrapper_is_narrow_and_normalizes_video_analysis(self) -> None:
        properties = _package()
        properties["video_analysis"] = "A prank on a commercial street."
        wrapper = {
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
                "h3_prompt",
            ],
            "properties": properties,
            "h3_prompt": "A complete shot-aware localization plan.",
        }
        malformed = json.dumps(wrapper, ensure_ascii=False) + ',"audio_path":"dub.mp4"}}'
        recovered = recover_doubao_schema_wrapper(malformed)
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(
            recovered["video_analysis"],
            {"summary": "A prank on a commercial street."},
        )
        self.assertEqual(recovered["h3_prompt"], "A complete shot-aware localization plan.")
        pure_recovered = recover_doubao_schema_wrapper(json.dumps(wrapper, ensure_ascii=False))
        self.assertIsNotNone(pure_recovered)
        assert pure_recovered is not None
        self.assertEqual(
            pure_recovered["h3_prompt"],
            "A complete shot-aware localization plan.",
        )

        active_properties = _package()
        active_properties["h3_prompt"] = "A complete v7 shot-aware localization plan."
        active_properties["reference_shots"] = [
            {
                "shot_id": "shot_001",
                "start_ms": 0,
                "end_ms": 6000,
                "keyframe_ms": 1000,
                "character_ids": ["char_visible_person"],
                "continuity_group": "main",
                "scene_description": "A complete localized street scene",
                "replacement_requirements": ["replace the facade and signs"],
                "preserve_requirements": ["preserve composition and action"],
                "seedream_prompt": "Edit the keyframe into the target-region scene",
            }
        ]
        active_wrapper = {
            "type": "object",
            "additionalProperties": False,
            "required": [*active_properties.keys()],
            "properties": active_properties,
        }
        active_recovered = recover_doubao_schema_wrapper(
            json.dumps(active_wrapper, ensure_ascii=False)
        )
        self.assertIsNotNone(active_recovered)
        assert active_recovered is not None
        self.assertEqual(active_recovered["reference_shots"][0]["shot_id"], "shot_001")
        truncated_recovered = recover_doubao_schema_wrapper(
            json.dumps(active_wrapper, ensure_ascii=False)[:-1]
        )
        self.assertIsNotNone(truncated_recovered)
        assert truncated_recovered is not None
        self.assertEqual(
            truncated_recovered["reference_shots"][0]["shot_id"],
            "shot_001",
        )

        class WrapperClient:
            @staticmethod
            def extract_text(response: ApiResponse) -> str:
                return extract_text(response)

            def chat(self, messages, **kwargs):
                return ApiResponse(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(active_wrapper, ensure_ascii=False)
                                }
                            }
                        ]
                    },
                    "wrapper-request",
                )

        analyzed = analyze_video(
            WrapperClient(),
            "https://example.test/source.mp4",
            target_language="ar",
            target_region="Gulf",
            target_locale="ar-SA",
            duration_seconds=6,
            require_h3_prompt=True,
            require_reference_plan=True,
        )
        self.assertEqual(analyzed.h3_prompt, active_properties["h3_prompt"])
        self.assertEqual(analyzed.reference_shots[0].shot_id, "shot_001")

    def test_normal_doubao_package_string_video_analysis_is_normalized(self) -> None:
        value = _package()
        value["video_analysis"] = "A short localized street scene."
        value["h3_prompt"] = "A complete v7 shot-aware localization plan."
        value["reference_shots"] = [
            {
                "shot_id": "shot_001",
                "start_ms": 0,
                "end_ms": 6000,
                "keyframe_ms": 1000,
                "character_ids": ["char_visible_person"],
                "continuity_group": "main",
                "scene_description": "A complete localized street scene",
                "replacement_requirements": ["replace the facade and signs"],
                "preserve_requirements": ["preserve composition and action"],
                "seedream_prompt": "Edit the keyframe into the target-region scene",
            }
        ]

        class StringAnalysisClient:
            @staticmethod
            def extract_text(response: ApiResponse) -> str:
                return extract_text(response)

            def chat(self, messages, **kwargs):
                return ApiResponse(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(value, ensure_ascii=False)
                                }
                            }
                        ]
                    },
                    "string-analysis-request",
                )

        analyzed = analyze_video(
            StringAnalysisClient(),
            "https://example.test/source.mp4",
            target_language="ar",
            target_region="Gulf",
            target_locale="ar-SA",
            duration_seconds=5.75,
            require_h3_prompt=True,
            require_reference_plan=True,
        )
        self.assertEqual(
            analyzed.video_analysis["summary"],
            "A short localized street scene.",
        )
        self.assertEqual(analyzed.reference_shots[0].end_ms, 5750)

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
