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
    overrides = {
        name: stored[name]
        for name in ("ark_api_key", "minimax_api_key")
        if not getattr(config, name) and stored.get(name)
    }
    if overrides:
        config = config.with_overrides(**overrides)
    run_gui(config)


if __name__ == "__main__":
    main()
