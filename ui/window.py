"""Minimal Tkinter surface for one MiniMax H3 video task."""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from collections.abc import Sequence
from pathlib import Path
from tkinter import filedialog, ttk

from config import AppConfig, MINIMAX_MAX_REFERENCE_IMAGES
from core.models import JobSpec, PipelineEvent, PipelineStage
from core.pipeline import VideoLocalizationPipeline
from language_config import (
    DEFAULT_TARGET_LOCALE_LABEL,
    H3_TARGET_LOCALES,
    locale_from_label,
)
from ui.concat import ConcatenateWindow
from ui.settings import LABEL_COLUMN_WIDTH, SettingsPanel


def append_reference_images(
    current_paths: Sequence[Path],
    selected_paths: Sequence[str | Path],
) -> tuple[Path, ...]:
    """Append new reference images without duplicates, up to the configured limit."""

    merged: list[Path] = []
    seen: set[str] = set()
    for raw_path in (*current_paths, *selected_paths):
        path = Path(raw_path).expanduser()
        key = str(path).casefold()
        if key not in seen:
            merged.append(path)
            seen.add(key)
    if len(merged) > MINIMAX_MAX_REFERENCE_IMAGES:
        raise ValueError(
            f"参考图最多添加 {MINIMAX_MAX_REFERENCE_IMAGES} 张，"
            f"当前已添加 {len(current_paths)} 张"
        )
    return tuple(merged)


class VideoLocalizerWindow(tk.Tk):
    def __init__(
        self,
        config: AppConfig,
        *,
        settings_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.title("视频本地化工具")
        self.geometry("760x290")
        self.minsize(700, 280)
        self.base_config = config
        self.settings_path = settings_path
        self.events: queue.Queue[PipelineEvent | None] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.pipeline: VideoLocalizationPipeline | None = None
        self.cancel_event = threading.Event()
        self._busy = False
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_events)

    def _build(self) -> None:
        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, minsize=LABEL_COLUMN_WIDTH)
        root.columnconfigure(1, weight=1)
        root.columnconfigure(3, minsize=100)
        root.rowconfigure(5, weight=1)

        self.settings = SettingsPanel(
            root,
            self.base_config,
            settings_path=self.settings_path,
            on_error=self._set_error,
        )
        self.settings.grid(row=3, column=0, columnspan=4, sticky="ew", pady=5)

        self.video_var = tk.StringVar()
        self.reference_var = tk.StringVar()
        self.reference_paths: tuple[Path, ...] = ()
        self.locale_var = tk.StringVar(value=DEFAULT_TARGET_LOCALE_LABEL)

        self.video_choose_button, self.video_clear_button = self._asset_row(
            root,
            1,
            "视频",
            self.video_var,
            self._choose_video,
            self._clear_video,
            [("视频", "*.mp4 *.mov *.mkv *.avi *.webm"), ("所有文件", "*.*")],
        )
        self.reference_choose_button, self.reference_clear_button = self._asset_row(
            root,
            2,
            "参考图（可选）",
            self.reference_var,
            self._choose_reference_images,
            self._clear_reference_images,
            [("图片", "*.jpg *.jpeg *.png *.webp *.heic *.heif"), ("所有文件", "*.*")],
            button_text="添加",
        )

        ttk.Label(root, text="目标地区").grid(
            row=0, column=0, sticky="w", padx=(0, 10), pady=5
        )
        self.locale_combo = ttk.Combobox(
            root,
            textvariable=self.locale_var,
            values=[locale.label for locale in H3_TARGET_LOCALES],
            state="readonly",
        )
        self.locale_combo.grid(row=0, column=1, columnspan=3, sticky="ew", pady=5)

        self.status_var = tk.StringVar(value="状态：待处理")
        ttk.Label(root, textvariable=self.status_var).grid(
            row=4, column=0, sticky="w", pady=(12, 0)
        )

        self.output_button = ttk.Button(
            root,
            text="打开输出目录",
            command=self._open_output,
        )
        self.output_button.grid(
            row=4, column=2, sticky="w", padx=(8, 0), pady=(12, 0)
        )
        self.concat_button = ttk.Button(
            root,
            text="拼接视频",
            command=self._open_concat_window,
        )
        self.concat_button.grid(
            row=4, column=3, sticky="w", padx=(8, 0), pady=(12, 0)
        )

        ttk.Label(
            root,
            text="Finjix 钟丰骏制作",
            foreground="#666666",
        ).grid(row=7, column=0, columnspan=4, sticky="s", pady=(2, 0))

        self.error_var = tk.StringVar()
        ttk.Label(
            root,
            textvariable=self.error_var,
            foreground="#b42318",
            wraplength=630,
            justify="left",
        ).grid(row=6, column=0, columnspan=4, sticky="w", pady=(2, 8))

        self.start_button = ttk.Button(
            root,
            text="开始处理",
            command=self._start,
        )
        self.start_button.grid(row=4, column=1, sticky="e", pady=(12, 0))

    def _asset_row(
        self,
        root: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        command,
        clear_command,
        filetypes: list[tuple[str, str]],
        *,
        button_text: str = "选择",
    ) -> tuple[ttk.Button, ttk.Button]:
        ttk.Label(root, text=label).grid(
            row=row, column=0, sticky="w", padx=(0, 10), pady=5
        )
        entry = ttk.Entry(root, textvariable=variable, state="readonly")
        entry.grid(row=row, column=1, sticky="ew", pady=5)
        button = ttk.Button(
            root,
            text=button_text,
            command=lambda: command(filetypes),
        )
        button.grid(row=row, column=2, sticky="e", padx=(8, 0), pady=5)
        clear_button = ttk.Button(
            root,
            text="清除",
            command=clear_command,
        )
        clear_button.grid(row=row, column=3, sticky="e", padx=(8, 0), pady=5)
        return button, clear_button

    def _choose_video(self, filetypes=None) -> None:
        path = filedialog.askopenfilename(
            title="选择视频",
            filetypes=filetypes
            or [
                ("视频", "*.mp4 *.mov *.mkv *.avi *.webm"),
                ("所有文件", "*.*"),
            ],
        )
        if path:
            self.video_var.set(path)
            self._clear_error()

    def _clear_video(self) -> None:
        self.video_var.set("")
        self._clear_error()

    def _choose_reference_images(self, filetypes=None) -> None:
        paths = filedialog.askopenfilenames(
            title=(
                f"添加参考图（按 Ctrl 或 Shift 可多选，最多 "
                f"{MINIMAX_MAX_REFERENCE_IMAGES} 张）"
            ),
            filetypes=filetypes
            or [
                ("图片", "*.jpg *.jpeg *.png *.webp *.heic *.heif"),
                ("所有文件", "*.*"),
            ],
        )
        if not paths:
            return
        try:
            reference_paths = append_reference_images(self.reference_paths, paths)
        except ValueError as exc:
            self._set_error(exc)
            return
        self.reference_paths = reference_paths
        names = "、".join(path.name for path in self.reference_paths)
        self.reference_var.set(
            f"已添加 {len(self.reference_paths)}/{MINIMAX_MAX_REFERENCE_IMAGES} 张：{names}"
        )
        self._clear_error()

    def _clear_reference_images(self) -> None:
        self.reference_paths = ()
        self.reference_var.set("")
        self._clear_error()

    def _build_spec(self) -> JobSpec:
        if not self.video_var.get().strip():
            raise ValueError("请选择视频")
        locale = locale_from_label(self.locale_var.get())
        if locale is None:
            raise ValueError("请选择目标地区")
        return JobSpec(
            input_video=Path(self.video_var.get().strip()),
            reference_images=self.reference_paths,
            target_locale=locale.locale_code,
        )

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        self._clear_error()
        try:
            spec = self._build_spec()
            config = self.base_config.with_overrides(
                minimax_api_key=self.settings.get_api_key()
            )
            self.settings.save()
            if not config.minimax_api_key:
                raise ValueError("请先填写 MiniMax API Key")
        except Exception as exc:
            self._set_error(exc)
            return

        self.status_var.set("状态：准备处理")
        self.cancel_event = threading.Event()
        self.pipeline = VideoLocalizationPipeline(
            config,
            event_callback=self.events.put,
            cancel_event=self.cancel_event,
        )
        self._set_busy(True)

        def worker() -> None:
            try:
                self.pipeline.run(spec)
            except Exception:
                pass
            finally:
                self.events.put(None)

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event is None:
                    self._set_busy(False)
                    continue
                self._handle_event(event)
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _handle_event(self, event: PipelineEvent) -> None:
        self.status_var.set(f"状态：{event.message}")
        if event.error:
            self._set_error(event.error)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.start_button.configure(state=state)
        self.video_choose_button.configure(state=state)
        self.video_clear_button.configure(state=state)
        self.reference_choose_button.configure(state=state)
        self.reference_clear_button.configure(state=state)
        self.concat_button.configure(state=state)
        self.locale_combo.configure(state="disabled" if busy else "readonly")
        self.settings.key_entry.configure(state=state)
        self.settings.save_button.configure(state=state)

    def _set_error(self, error: object) -> None:
        message = " ".join(str(error).split())
        if len(message) > 300:
            message = message[:297] + "..."
        self.error_var.set(message)

    def _clear_error(self) -> None:
        self.error_var.set("")

    def _open_output(self) -> None:
        target = Path(self.base_config.output_dir)
        try:
            target.mkdir(parents=True, exist_ok=True)
            os.startfile(str(target))  # type: ignore[attr-defined]
        except OSError as exc:
            self._set_error(f"无法打开输出目录: {exc}")

    def _open_concat_window(self) -> None:
        if self._busy:
            return
        ConcatenateWindow(self, self.base_config)

    def _on_close(self) -> None:
        try:
            self.settings.save()
        except OSError:
            pass
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
        self.destroy()


def run_gui(config: AppConfig, *, settings_path: Path | None = None) -> None:
    VideoLocalizerWindow(config, settings_path=settings_path).mainloop()
