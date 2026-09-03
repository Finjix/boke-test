"""The only configurable credential exposed by the desktop UI."""

from __future__ import annotations

from pathlib import Path
import tkinter as tk
from tkinter import ttk
from typing import Callable

from config import AppConfig
from utils.settings_store import SETTINGS_FILENAME, SettingsStore


LABEL_COLUMN_WIDTH = 140


class SettingsPanel(ttk.Frame):
    def __init__(
        self,
        master: tk.Misc,
        config: AppConfig,
        *,
        settings_path: Path | None = None,
        on_error: Callable[[object], None] | None = None,
    ) -> None:
        super().__init__(master, padding=0)
        root = Path(__file__).resolve().parents[1]
        self._store = SettingsStore(
            settings_path or root / SETTINGS_FILENAME
        )
        self._on_error = on_error
        stored = self._store.load()
        self.key_var = tk.StringVar(
            value=stored.get("minimax_api_key") or config.minimax_api_key
        )
        ttk.Label(self, text="MiniMax API Key").grid(
            row=0, column=0, sticky="w", padx=(0, 10)
        )
        self.key_entry = ttk.Entry(self, textvariable=self.key_var, show="*")
        self.key_entry.grid(row=0, column=1, sticky="ew")
        self.save_button = ttk.Button(
            self,
            text="保存",
            command=self._save_from_ui,
        )
        self.save_button.grid(row=0, column=2, padx=(8, 0))
        self.columnconfigure(0, minsize=LABEL_COLUMN_WIDTH)
        self.columnconfigure(1, weight=1)

    def get_api_key(self) -> str:
        return self.key_var.get().strip()

    def save(self) -> None:
        self._store.save({"minimax_api_key": self.get_api_key()})

    def _save_from_ui(self) -> None:
        try:
            self.save()
        except Exception as exc:
            if self._on_error is not None:
                self._on_error(exc)
            else:
                raise
