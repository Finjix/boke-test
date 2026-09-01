"""Tkinter settings panel for provider credentials and model configuration."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from config import AppConfig


class SettingsPanel(ttk.LabelFrame):
    def __init__(self, master: tk.Misc, config: AppConfig):
        super().__init__(master, text="API 设置", padding=8)
        self._vars: dict[str, tk.StringVar] = {}
        rows = (
            ("Ark API Key", "ark_api_key", True, ""),
            ("MediaKit API Key", "mediakit_api_key", True, ""),
            ("Seed-Audio API Key", "seed_audio_api_key", True, ""),
            ("Seedance Model/Endpoint ID", "seedance_model_id", False, config.seedance_model_id),
            ("高级：Uguu 上传 URL", "uguu_upload_url", False, config.uguu_upload_url),
        )
        for row, (label, name, secret, initial) in enumerate(rows):
            ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            variable = tk.StringVar(value=initial)
            self._vars[name] = variable
            entry = ttk.Entry(self, textvariable=variable, width=48, show="*" if secret else "")
            entry.grid(row=row, column=1, sticky="ew", pady=3)
        self.columnconfigure(1, weight=1)

    def get_non_empty_overrides(self) -> dict[str, Any]:
        return {
            name: variable.get().strip()
            for name, variable in self._vars.items()
            if variable.get().strip()
        }
