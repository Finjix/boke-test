"""Tkinter settings panel for the active MiniMax H3 workflow."""

from __future__ import annotations

from pathlib import Path
import tkinter as tk
from tkinter import ttk
from typing import Any

from config import AppConfig
from utils.settings_store import H3_SETTING_NAMES, SettingsStore


class SettingsPanel(ttk.LabelFrame):
    def __init__(
        self,
        master: tk.Misc,
        config: AppConfig,
        *,
        settings_path: Path | None = None,
    ) -> None:
        super().__init__(master, text="MiniMax H3 设置", padding=8)
        self._store = SettingsStore(settings_path)
        stored = self._store.load()
        self._vars: dict[str, tk.StringVar] = {}

        ttk.Label(self, text="MiniMax API Key").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=3
        )
        variable = tk.StringVar(value=stored.get("minimax_api_key", config.minimax_api_key))
        self._vars["minimax_api_key"] = variable
        ttk.Entry(self, textvariable=variable, width=48, show="*").grid(
            row=0, column=1, sticky="ew", pady=3
        )
        ttk.Label(
            self,
            text="模型：MiniMax-H3    默认分辨率：768P    端点：https://api.minimax.cn",
        ).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(5, 0)
        )
        ttk.Label(self, text="密钥只保存在项目根目录本地设置文件，不写入任务日志。").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(2, 0)
        )
        self.columnconfigure(1, weight=1)

    def get_overrides(self) -> dict[str, str]:
        return {
            name: self._vars[name].get().strip()
            for name in H3_SETTING_NAMES
            if name in self._vars
        }

    def get_non_empty_overrides(self) -> dict[str, Any]:
        return {name: value for name, value in self.get_overrides().items() if value}

    def get_auto_continue_to_seedance(self) -> bool:
        """Compatibility method; H3 starts directly and has no approval toggle."""

        return False

    def save(self) -> None:
        self._store.save(self.get_overrides())
