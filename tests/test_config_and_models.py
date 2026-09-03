from __future__ import annotations

import unittest
from pathlib import Path

from config import AppConfig, FIXED_DOUBAO_MODEL, FIXED_SEEDREAM_MODEL
from core.models import (
    JobSpec,
    LocalizationDialogue,
    LocalizationPackage,
    LocalizationSpeaker,
)
from language_config import DEFAULT_TARGET_LOCALE_LABEL, TARGET_LOCALES, locale_from_label
from utils.errors import ConfigurationError


def _package() -> LocalizationPackage:
    return LocalizationPackage(
        source={"language": "en"},
        target={"language": "ar", "region": "Gulf", "locale": "ar-SA"},
        video_analysis={"theme": "conversation"},
        speakers=[LocalizationSpeaker(id="speaker_1", visual_hint="left person")],
        dialogues=[
            LocalizationDialogue(
                speaker_id="speaker_1",
                start_ms=1200,
                end_ms=2850,
                source_text="Hello",
                target_text="مرحبا",
            )
        ],
        visual_localization={"characters": "modern Gulf business attire"},
        cultural_requirements=["Use a natural Gulf-market setting"],
    )


class ConfigAndModelsTests(unittest.TestCase):
    def test_config_reads_documented_values_without_secrets_in_repr(self) -> None:
        config = AppConfig.from_env(
            {
                "ARK_API_KEY": "secret-value",
                "DOUBAO_MODEL": FIXED_DOUBAO_MODEL,
                "WORK_DIR": "custom-work",
            },
            base_dir=Path("D:/example"),
            load_file=False,
        )
        self.assertEqual(config.work_dir, Path("D:/example/custom-work"))
        self.assertNotIn("secret-value", repr(config))

    def test_retired_audio_and_ffmpeg_settings_are_not_runtime_fields(self) -> None:
        fields = AppConfig.__dataclass_fields__
        self.assertNotIn("seed_audio_api_key", fields)
        self.assertNotIn("seed_audio_endpoint", fields)
        self.assertNotIn("seed_audio_model", fields)
        self.assertNotIn("ffmpeg_bin", fields)
        config = AppConfig.from_env(
            {
                "SEED_AUDIO_API_KEY": "ignored",
                "SEED_AUDIO_MODEL": "ignored",
            },
            load_file=False,
        )
        self.assertEqual(config.missing_runtime_values(), ["ARK_API_KEY", "SEEDANCE_MODEL_ID"])

    def test_active_h3_runtime_values_require_both_analysis_and_generation_keys(self) -> None:
        config = AppConfig()
        self.assertEqual(
            config.missing_h3_runtime_values(), ["ARK_API_KEY", "MINIMAX_API_KEY"]
        )
        configured = AppConfig(ark_api_key="ark", minimax_api_key="h3")
        self.assertEqual(configured.missing_h3_runtime_values(), [])

    def test_model_id_is_fixed(self) -> None:
        with self.assertRaises(ConfigurationError):
            AppConfig.from_env({"DOUBAO_MODEL": "another-model"}, load_file=False)
        self.assertEqual(FIXED_SEEDREAM_MODEL, "doubao-seedream-5-0-pro-260628")
        with self.assertRaises(ConfigurationError):
            AppConfig(seedream_model="another-model").validate_values()

    def test_target_catalog_has_required_first_phase_languages(self) -> None:
        labels = [locale.label for locale in TARGET_LOCALES]
        self.assertIn(DEFAULT_TARGET_LOCALE_LABEL, labels)
        self.assertEqual(locale_from_label(DEFAULT_TARGET_LOCALE_LABEL).locale_code, "ar-SA")  # type: ignore[union-attr]
        required_codes = {"en", "zh", "ja", "ko", "es", "fr", "de", "pt", "ru", "ar"}
        self.assertTrue(required_codes.issubset({locale.language_code for locale in TARGET_LOCALES}))
        self.assertIn("zh-CN", {locale.locale_code for locale in TARGET_LOCALES})

    def test_localization_package_maps_dialogues_to_speakers_and_target(self) -> None:
        package = _package()
        self.assertEqual(package.source_language, "en")
        self.assertEqual(package.target_language, "ar")
        self.assertEqual(package.target_region, "Gulf")
        self.assertEqual(package.target_locale, "ar-SA")
        self.assertEqual(package.dialogues[0].speaker_id, "speaker_1")

    def test_localization_package_rejects_missing_speaker(self) -> None:
        raw = _package().model_dump(mode="json")
        raw["dialogues"][0]["speaker_id"] = "speaker_missing"
        with self.assertRaises(ValueError):
            LocalizationPackage.model_validate(raw)

    def test_job_spec_requires_target_locale_and_has_no_source_fields(self) -> None:
        spec = JobSpec(
            input_video="input.mp4",
            target_language="ar",
            target_region="Gulf",
            target_locale="ar-SA",
        )
        self.assertEqual(spec.target_locale, "ar-SA")
        self.assertNotIn("source_language", JobSpec.model_fields)
        self.assertNotIn("source_region", JobSpec.model_fields)
        self.assertNotIn("source_asr_language", JobSpec.model_fields)

    def test_job_spec_uses_language_code_and_bcp47_locale(self) -> None:
        named = JobSpec(
            input_video="input.mp4",
            target_language="Arabic",
            target_region="Gulf",
            target_locale="ar-SA",
        )
        self.assertEqual(named.target_language, "ar")
        with self.assertRaises(ValueError):
            JobSpec(
                input_video="input.mp4",
                target_language="ar",
                target_region="Gulf",
                target_locale="Arabic",
            )
