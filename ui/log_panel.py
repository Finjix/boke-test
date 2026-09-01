"""Scrollable live log panel."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk


class LogPanel(ttk.LabelFrame):
    def __init__(self, master: tk.Misc):
        super().__init__(master, text="实时日志", padding=6)
        self.text = tk.Text(self, height=12, width=100, state="disabled", wrap="word")
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scrollbar.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

    def append(self, message: str) -> None:
        self.text.configure(state="normal")
        self.text.insert("end", message.rstrip() + "\n")
        self.text.see("end")
        self.text.configure(state="disabled")
