"""Persistence for settings entered in the desktop UI.

The user explicitly opted to keep API credentials between application runs.
Values are stored as a small local JSON file in the project root. The file is
ignored by Git and is never sent to a provider or written to the job log.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping


SETTINGS_FILENAME = ".video-localizer-settings.json"
EDITABLE_SETTING_NAMES = (
    "ark_api_key",
    "seedance_model_id",
)
PREFERENCE_NAMES = (
    "auto_continue_to_seedance",
)


class SettingsStore:
    """Read and atomically write the values exposed by the GUI settings panel."""

    def __init__(self, path: Path | None = None, *, project_root: Path | None = None):
        root = Path(project_root or Path(__file__).resolve().parents[1])
        self.path = Path(path) if path is not None else root / SETTINGS_FILENAME

    def load(self) -> dict[str, str]:
        """Load valid editable settings, ignoring a missing or damaged file."""

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}

        values = payload.get("values", payload) if isinstance(payload, dict) else {}
        if not isinstance(values, dict):
            return {}
        return {
            name: value
            for name in EDITABLE_SETTING_NAMES
            if isinstance(value := values.get(name), str)
        }

    def load_preferences(self) -> dict[str, bool]:
        """Load non-secret UI preferences with safe defaults."""

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        preferences = payload.get("preferences", {}) if isinstance(payload, dict) else {}
        if not isinstance(preferences, dict):
            return {}
        result: dict[str, bool] = {}
        for name in PREFERENCE_NAMES:
            value = preferences.get(name)
            if isinstance(value, bool):
                result[name] = value
            elif isinstance(value, str) and value.strip().casefold() in {"1", "true", "yes", "on"}:
                result[name] = True
            elif isinstance(value, str) and value.strip().casefold() in {"0", "false", "no", "off"}:
                result[name] = False
        return result

    def save(
        self,
        values: Mapping[str, str],
        *,
        preferences: Mapping[str, bool] | None = None,
    ) -> None:
        """Persist editable settings without leaving a partially written file."""

        normalized = {
            name: str(values.get(name, "")).strip()
            for name in EDITABLE_SETTING_NAMES
        }
        old_preferences = self.load_preferences()
        normalized_preferences = {
            name: bool(
                (preferences or {}).get(
                    name,
                    old_preferences.get(name, False),
                )
            )
            for name in PREFERENCE_NAMES
        }
        payload = {
            "version": 3,
            "values": normalized,
            "preferences": normalized_preferences,
        }
        temporary_path = self.path.with_name(f".{self.path.name}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary_path.replace(self.path)
        except OSError:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
