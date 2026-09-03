"""Application entry point."""

from __future__ import annotations

from pathlib import Path

from config import AppConfig
from ui.window import run_gui
from utils.settings_store import SettingsStore


def main() -> None:
    project_root = Path(__file__).resolve().parent
    config = AppConfig.from_env(base_dir=project_root)
    stored = SettingsStore(project_root=project_root).load()
    if not config.minimax_api_key and stored.get("minimax_api_key"):
        config = config.with_overrides(
            minimax_api_key=stored["minimax_api_key"]
        )
    run_gui(config)


if __name__ == "__main__":
    main()
