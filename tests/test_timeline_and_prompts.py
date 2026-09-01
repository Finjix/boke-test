from __future__ import annotations

import json
import unittest

from core.models import JobSpec, Segment, SpeakerProfile
from core.seed_audio_prompt import build_seed_audio_prompt
from core.seedance_prompt import build_seedance_content
from core.speaker import parse_speaker_profile, select_speaker_anchors
from core.timeline import normalize_asr, validate_translation
from utils.errors import ValidationError
from video_config import atempo_factor, timing_is_acceptable


class TimelineAndPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.segments = normalize_asr(
            {
                "result": {
                    "subtitles": [
                        {
                            "start_time": 0.52,
                            "end_time": 2.81,
                            "subtitle_text": "Hello",
                            "speaker": "speaker_0",
                            "confidence": 0.97,
                        },
                        {
                            "start_time": 3.1,
                            "end_time": 5.45,
                            "subtitle_text": "Hi",
                            "speaker": "speaker_1",
                            "confidence": 0.9,
                        },
                    ]
                }
            }
        )
        self.profiles = [
            SpeakerProfile(
                speaker_id="speaker_0",
                gender="male",
                age_group="young",
                role_type="main_character",
                voice_style=["low", "calm"],
            ),
            SpeakerProfile(
                speaker_id="speaker_1",
                gender="female",
                age_group="young",
                role_type="supporting",
                voice_style=["bright", "energetic"],
            ),
        ]

    def test_translation_preserves_contract(self) -> None:
        translated = [
            {
                "id": "seg_0001",
                "speaker": "speaker_0",
                "start": 0.52,
                "end": 2.81,
                "text": "مرحبا",
            },
            {
                "id": "seg_0002",
                "speaker": "speaker_1",
                "start": 3.1,
                "end": 5.45,
                "text": "أهلا",
            },
        ]
        result = validate_translation(self.segments, translated)
        self.assertEqual(result[0].text, "مرحبا")

    def test_translation_timestamp_change_is_rejected(self) -> None:
        translated = [item.model_dump() for item in self.segments]
        translated[0]["start"] = 0.7
        with self.assertRaises(ValidationError):
            validate_translation(self.segments, translated)

    def test_speaker_analysis_normalizes_unknown_values(self) -> None:
        profile = parse_speaker_profile(
            "speaker_0",
            json.dumps(
                {
                    "gender": "not-an-enum",
                    "age_group": "young",
                    "role_type": "main_character",
                    "voice_style": "restrained",
                    "confidence": 0.8,
                }
            ),
        )
        self.assertEqual(profile.gender, "unknown")
        self.assertEqual(profile.voice_style, ["restrained"])

    def test_seed_audio_prompt_is_dry_dialogue_only(self) -> None:
        prompt = build_seed_audio_prompt(
            self.segments,
            self.profiles,
            duration=6,
            target_language="Arabic",
            target_region="Gulf",
        )
        self.assertIn("DRY DIALOGUE ONLY", prompt)
        self.assertIn("不生成背景音乐", prompt)
        self.assertIn("speaker_0", prompt)
        self.assertNotIn("voice_id", prompt)

    def test_seedance_content_contains_video_audio_and_refs(self) -> None:
        job = JobSpec(
            input_video="input.mp4",
            target_language="English",
            target_region="United States",
        )
        from core.models import UploadedAsset

        asset = UploadedAsset(
            local_path="ref.png",
            remote_url="https://uguu.se/ref.png",
            uploaded_at="now",
            kind="character_reference",
        )
        content = build_seedance_content(
            "https://uguu.se/input.mp4",
            "https://uguu.se/voice.wav",
            [asset],
            job,
        )
        self.assertEqual([item["type"] for item in content], ["text", "video_url", "audio_url", "image_url"])
        self.assertEqual(content[-1]["role"], "reference_image")

    def test_anchor_selection_uses_start_middle_end(self) -> None:
        anchors = select_speaker_anchors(self.segments, 8)
        self.assertEqual(len(anchors), 2)
        self.assertAlmostEqual(anchors[0].middle, (0.52 + 2.81) / 2)

    def test_audio_timing_boundaries_are_inclusive(self) -> None:
        self.assertTrue(timing_is_acceptable(0.97))
        self.assertTrue(timing_is_acceptable(1.03))
        self.assertFalse(timing_is_acceptable(0.9699))
        self.assertAlmostEqual(atempo_factor(10.3, 10), 1.03)
