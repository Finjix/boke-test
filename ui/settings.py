"""Tkinter settings panel for the active Doubao + MiniMax H3 workflow."""

from __future__ import annotations

from pathlib import Path
import tkinter as tk
from tkinter import ttk
from typing import Any

from config import AppConfig
from utils.settings_store import EDITABLE_SETTING_NAMES, H3_SETTING_NAMES, SettingsStore


class SettingsPanel(ttk.LabelFrame):
    def __init__(
        self,
        master: tk.Misc,
        config: AppConfig,
        *,
        settings_path: Path | None = None,
    ) -> None:
        super().__init__(master, text="Doubao Seed + MiniMax H3 设置", padding=8)
        self._store = SettingsStore(settings_path)
        stored = self._store.load()
        preferences = self._store.load_preferences()
        self._vars: dict[str, tk.StringVar] = {}

        ttk.Label(self, text="Doubao / Ark API Key").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=3
        )
        ark_variable = tk.StringVar(value=stored.get("ark_api_key", config.ark_api_key))
        self._vars["ark_api_key"] = ark_variable
        ttk.Entry(self, textvariable=ark_variable, width=48, show="*").grid(
            row=0, column=1, sticky="ew", pady=3
        )
        ttk.Label(self, text="MiniMax H3 API Key").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=3
        )
        minimax_variable = tk.StringVar(
            value=stored.get("minimax_api_key", config.minimax_api_key)
        )
        self._vars["minimax_api_key"] = minimax_variable
        ttk.Entry(self, textvariable=minimax_variable, width=48, show="*").grid(
            row=1, column=1, sticky="ew", pady=3
        )
        self.auto_continue_var = tk.BooleanVar(
            value=bool(preferences.get("auto_continue_to_seedance", False))
        )
        ttk.Checkbutton(
            self,
            text="Doubao 完成后自动进入 H3（跳过方案和参考图两次确认）",
            variable=self.auto_continue_var,
        ).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(5, 0)
        )
        self.columnconfigure(1, weight=1)

    def get_overrides(self) -> dict[str, str]:
        return {
            name: self._vars[name].get().strip()
            for name in (*EDITABLE_SETTING_NAMES, *H3_SETTING_NAMES)
            if name in self._vars
        }

    def get_non_empty_overrides(self) -> dict[str, Any]:
        return {name: value for name, value in self.get_overrides().items() if value}

    def get_auto_continue_to_seedance(self) -> bool:
        """Keep the historical preference name while applying it to H3."""

        return bool(self.auto_continue_var.get())

    def get_auto_continue_to_h3(self) -> bool:
        """Return the active v7 preference using its current terminology."""

        return self.get_auto_continue_to_seedance()

    def save(self) -> None:
        self._store.save(
            self.get_overrides(),
            preferences={
                "auto_continue_to_seedance": self.get_auto_continue_to_seedance(),
            },
        )
