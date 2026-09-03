"""Tkinter settings panel for the MiniMax-only workflow."""

from __future__ import annotations

from pathlib import Path
import tkinter as tk
from tkinter import ttk
from typing import Any

from config import AppConfig
from utils.settings_store import EDITABLE_SETTING_NAMES, SettingsStore


class SettingsPanel(ttk.LabelFrame):
    """Expose the single credential needed by the active pipeline."""

    def __init__(
        self,
        master: tk.Misc,
        config: AppConfig,
        *,
        settings_path: Path | None = None,
    ) -> None:
        super().__init__(master, text="MiniMax 设置", padding=8)
        self._store = SettingsStore(settings_path)
        stored = self._store.load()
        self._vars: dict[str, tk.StringVar] = {}

        ttk.Label(self, text="MiniMax API Key").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=3
        )
        variable = tk.StringVar(
            value=stored.get("minimax_api_key") or config.minimax_api_key
        )
        self._vars["minimax_api_key"] = variable
        ttk.Entry(self, textvariable=variable, width=58, show="*").grid(
            row=0, column=1, sticky="ew", pady=3
        )
        self.columnconfigure(1, weight=1)

    def get_overrides(self) -> dict[str, str]:
        return {
            name: self._vars[name].get().strip()
            for name in EDITABLE_SETTING_NAMES
            if name in self._vars
        }

    def get_non_empty_overrides(self) -> dict[str, Any]:
        return {name: value for name, value in self.get_overrides().items() if value}

    def save(self) -> None:
        self._store.save(self.get_overrides())
