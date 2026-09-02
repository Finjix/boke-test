from __future__ import annotations

import unittest
from pathlib import Path

from config import AppConfig, FIXED_DOUBAO_MODEL, FIXED_SEED_AUDIO_MODEL
from core.models import JobSpec, LocalizationDialogue, LocalizationScript, LocalizationSpeaker
from language_config import DEFAULT_TARGET_LOCALE_LABEL, TARGET_LOCALES, locale_from_label
from utils.errors import ConfigurationError


class ConfigAndModelsTests(unittest.TestCase):
    def test_config_reads_documented_values_without_secrets_in_repr(self) -> None:
        config = AppConfig.from_env(
            {
                "ARK_API_KEY": "secret-value",
                "DOUBAO_MODEL": FIXED_DOUBAO_MODEL,
                "SEED_AUDIO_MODEL": FIXED_SEED_AUDIO_MODEL,
                "WORK_DIR": "custom-work",
            },
            base_dir=Path("D:/example"),
            load_file=False,
        )
        self.assertEqual(config.work_dir, Path("D:/example/custom-work"))
        self.assertNotIn("secret-value", repr(config))

    def test_model_ids_are_fixed(self) -> None:
        with self.assertRaises(ConfigurationError):
            AppConfig.from_env({"DOUBAO_MODEL": "another-model"}, load_file=False)
        with self.assertRaises(ConfigurationError):
            AppConfig.from_env({"SEED_AUDIO_MODEL": "another-model"}, load_file=False)

    def test_target_catalog_has_locale_codes_without_source_filtering(self) -> None:
        labels = [locale.label for locale in TARGET_LOCALES]
        self.assertIn(DEFAULT_TARGET_LOCALE_LABEL, labels)
        self.assertEqual(locale_from_label(DEFAULT_TARGET_LOCALE_LABEL).locale_code, "ar-SA")  # type: ignore[union-attr]
        self.assertIn("ja-JP", {locale.locale_code for locale in TARGET_LOCALES})
        self.assertIn("ko-KR", {locale.locale_code for locale in TARGET_LOCALES})

    def test_localization_script_maps_dialogues_to_speakers(self) -> None:
        script = LocalizationScript(
            source_language="en",
            target_language="ar",
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
        )
        self.assertEqual(script.dialogues[0].speaker_id, "speaker_1")
        self.assertEqual(script.dialogues[0].start_ms, 1200)

    def test_localization_script_rejects_missing_speaker(self) -> None:
        with self.assertRaises(ValueError):
            LocalizationScript(
                source_language="en",
                target_language="ar",
                speakers=[],
                dialogues=[
                    {
                        "speaker_id": "speaker_1",
                        "start_ms": 0,
                        "end_ms": 1000,
                        "source_text": "Hello",
                        "target_text": "مرحبا",
                    }
                ],
            )

    def test_job_spec_requires_target_locale_and_has_no_source_locale_fields(self) -> None:
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
        with self.assertRaises(ValueError):
            JobSpec(
                input_video="input.mp4",
                target_language="Arabic",
                target_region="Gulf",
                target_locale="ar-SA",
            )
        with self.assertRaises(ValueError):
            JobSpec(
                input_video="input.mp4",
                target_language="ar",
                target_region="Gulf",
                target_locale="Arabic",
            )
