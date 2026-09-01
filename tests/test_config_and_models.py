from __future__ import annotations

import unittest
from pathlib import Path

from config import AppConfig, FIXED_SEED_AUDIO_MODEL
from core.models import Segment, SpeakerProfile
from utils.errors import ConfigurationError


class ConfigAndModelsTests(unittest.TestCase):
    def test_config_reads_documented_values_without_secrets_in_repr(self) -> None:
        config = AppConfig.from_env(
            {
                "ARK_API_KEY": "secret-value",
                "SEED_AUDIO_MODEL": FIXED_SEED_AUDIO_MODEL,
                "WORK_DIR": "custom-work",
            },
            base_dir=Path("D:/example"),
            load_file=False,
        )
        self.assertEqual(config.work_dir, Path("D:/example/custom-work"))
        self.assertNotIn("secret-value", repr(config))

    def test_non_fixed_audio_model_is_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            AppConfig.from_env(
                {"SEED_AUDIO_MODEL": "another-model"},
                load_file=False,
            )

    def test_segment_duration_is_derived_and_profile_enums_are_typed(self) -> None:
        segment = Segment(
            id="seg_0001",
            speaker="speaker_0",
            start=0.5,
            end=2.0,
            text="Hello",
        )
        self.assertAlmostEqual(segment.duration or 0, 1.5)
        profile = SpeakerProfile(
            speaker_id="speaker_0",
            gender="male",
            age_group="young",
            role_type="main_character",
            voice_style=["calm"],
        )
        self.assertEqual(profile.role_type, "main_character")

