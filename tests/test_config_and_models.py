from __future__ import annotations

import unittest
from pathlib import Path

from config import AppConfig, FIXED_SEED_AUDIO_MODEL
from core.models import Segment, SpeakerProfile
from language_config import DEFAULT_TARGET_LOCALE_LABEL, source_locale_from_label, target_locales_for_source
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

    def test_source_region_filters_same_target_and_sets_asr_language(self) -> None:
        source = source_locale_from_label("United States (English)")
        self.assertIsNotNone(source)
        assert source is not None
        self.assertEqual(source.asr_language, "eng-US")
        labels = [locale.label for locale in target_locales_for_source(source)]
        self.assertNotIn("United States (English)", labels)
        self.assertIn("United Kingdom (English)", labels)

    def test_default_target_locale_is_available(self) -> None:
        labels = [locale.label for locale in target_locales_for_source(None)]
        self.assertIn(DEFAULT_TARGET_LOCALE_LABEL, labels)

    def test_job_spec_rejects_same_source_and_target_region(self) -> None:
        with self.assertRaises(ValueError):
            from core.models import JobSpec

            JobSpec(
                input_video="input.mp4",
                source_language="English",
                source_region="United States",
                source_asr_language="eng-US",
                target_language="English",
                target_region="United States",
            )
