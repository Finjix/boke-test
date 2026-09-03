"""Small local persistence for the MiniMax API key entered in Tkinter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping


SETTINGS_FILENAME = ".video-localizer-settings.json"
SETTINGS_VERSION = 1
EDITABLE_SETTING_NAMES = ("minimax_api_key",)


class SettingsStore:
    """Read and atomically write the only setting exposed by the GUI."""

    def __init__(self, path: Path | None = None, *, project_root: Path | None = None):
        root = Path(project_root or Path(__file__).resolve().parents[1])
        self.path = Path(path) if path is not None else root / SETTINGS_FILENAME

    def load(self) -> dict[str, str]:
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

    def save(self, values: Mapping[str, str]) -> None:
        normalized = {
            name: str(values.get(name, "")).strip()
            for name in EDITABLE_SETTING_NAMES
        }
        payload = {"version": SETTINGS_VERSION, "values": normalized}
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
