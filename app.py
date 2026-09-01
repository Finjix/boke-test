"""Application entry point."""

from __future__ import annotations

from pathlib import Path

from config import AppConfig
from ui.window import run_gui


def main() -> None:
    project_root = Path(__file__).resolve().parent
    run_gui(AppConfig.from_env(base_dir=project_root))


if __name__ == "__main__":
    main()
