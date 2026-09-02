"""Tkinter settings panel for provider credentials and model configuration."""

from __future__ import annotations

from pathlib import Path
import tkinter as tk
from tkinter import ttk
from typing import Any

from config import AppConfig
from utils.settings_store import EDITABLE_SETTING_NAMES, SettingsStore


class SettingsPanel(ttk.LabelFrame):
    def __init__(
        self,
        master: tk.Misc,
        config: AppConfig,
        *,
        settings_path: Path | None = None,
    ):
        super().__init__(master, text="API 设置", padding=8)
        self._vars: dict[str, tk.StringVar] = {}
        self._store = SettingsStore(settings_path)
        stored = self._store.load()
        rows = (
            ("Ark API Key", "ark_api_key", True, config.ark_api_key),
            ("Seedance Model/Endpoint ID", "seedance_model_id", False, config.seedance_model_id),
        )
        for row, (label, name, secret, initial) in enumerate(rows):
            ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            variable = tk.StringVar(value=stored.get(name, initial))
            self._vars[name] = variable
            entry = ttk.Entry(self, textvariable=variable, width=48, show="*" if secret else "")
            entry.grid(row=row, column=1, sticky="ew", pady=3)
        self.columnconfigure(1, weight=1)

    def get_overrides(self) -> dict[str, str]:
        """Return all editable values, including deliberate empty values."""

        return {
            name: self._vars[name].get().strip()
            for name in EDITABLE_SETTING_NAMES
            if name in self._vars
        }

    def get_non_empty_overrides(self) -> dict[str, Any]:
        """Backward-compatible helper that omits empty values."""

        return {name: value for name, value in self.get_overrides().items() if value}

    def save(self) -> None:
        """Save the current fields for the next application launch."""

        self._store.save(self.get_overrides())
