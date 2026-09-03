"""Application entry point."""

from __future__ import annotations

from pathlib import Path

from config import AppConfig
from ui.window import run_gui
from utils.settings_store import SETTINGS_FILENAME, SettingsStore


def main() -> None:
    project_root = Path(__file__).resolve().parent
    config = AppConfig.from_env(base_dir=project_root)
    settings_path = project_root / SETTINGS_FILENAME
    stored = SettingsStore(settings_path).load()
    if not config.minimax_api_key and stored.get("minimax_api_key"):
        config = config.with_overrides(minimax_api_key=stored["minimax_api_key"])
    run_gui(config, settings_path=settings_path)


if __name__ == "__main__":
    main()
