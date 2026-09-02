from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from utils.settings_store import EDITABLE_SETTING_NAMES, SettingsStore


class SettingsStoreTests(unittest.TestCase):
    def test_round_trip_keeps_v2_gui_values_and_ignores_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".video-localizer-settings.json"
            store = SettingsStore(path)
            values = {
                "ark_api_key": "ark-secret",
                "seed_audio_api_key": "audio-secret",
                "seedance_model_id": "ep-test",
            }

            store.save({**values, "uguu_upload_url": "https://example.invalid"})

            self.assertEqual(store.load(), values)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 2)
            self.assertEqual(tuple(payload["values"]), EDITABLE_SETTING_NAMES)
            self.assertNotIn("mediakit_api_key", payload["values"])

    def test_missing_or_invalid_store_is_treated_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".video-localizer-settings.json"
            store = SettingsStore(path)
            self.assertEqual(store.load(), {})

            path.write_text("not-json", encoding="utf-8")
            self.assertEqual(store.load(), {})

    def test_empty_values_are_persisted_so_a_user_can_clear_a_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.json")
            store.save({"ark_api_key": ""})
            self.assertEqual(store.load()["ark_api_key"], "")
