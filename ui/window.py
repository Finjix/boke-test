"""Minimal Tkinter surface for one MiniMax H3 video task."""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from config import APP_VERSION, AppConfig
from core.models import JobSpec, PipelineEvent, PipelineStage
from core.pipeline import VideoLocalizationPipeline
from language_config import (
    DEFAULT_TARGET_LOCALE_LABEL,
    H3_TARGET_LOCALES,
    locale_from_label,
)
from ui.settings import SettingsPanel


class VideoLocalizerWindow(tk.Tk):
    def __init__(
        self,
        config: AppConfig,
        *,
        settings_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.title(f"MiniMax H3 视频处理 v{APP_VERSION}")
        self.geometry("680x520")
        self.minsize(600, 460)
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
        root.columnconfigure(1, weight=1)

        ttk.Label(
            root,
            text=f"MiniMax H3 视频处理   v{APP_VERSION}",
            font=("Segoe UI", 12, "bold"),
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))

        self.settings = SettingsPanel(
            root,
            self.base_config,
            settings_path=self.settings_path,
            on_error=self._set_error,
        )
        self.settings.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 10))

        self.video_var = tk.StringVar()
        self.person_var = tk.StringVar()
        self.scene_var = tk.StringVar()
        self.locale_var = tk.StringVar(value=DEFAULT_TARGET_LOCALE_LABEL)

        self.video_choose_button = self._asset_row(
            root,
            2,
            "视频",
            self.video_var,
            self._choose_video,
            [("视频", "*.mp4 *.mov *.mkv *.avi *.webm"), ("所有文件", "*.*")],
        )
        self.person_choose_button = self._asset_row(
            root,
            3,
            "人物图",
            self.person_var,
            self._choose_person_image,
            [("图片", "*.jpg *.jpeg *.png *.webp *.heic *.heif"), ("所有文件", "*.*")],
        )
        self.scene_choose_button = self._asset_row(
            root,
            4,
            "场景图",
            self.scene_var,
            self._choose_scene_image,
            [("图片", "*.jpg *.jpeg *.png *.webp *.heic *.heif"), ("所有文件", "*.*")],
        )

        ttk.Label(root, text="目标地区").grid(
            row=5, column=0, sticky="w", padx=(0, 10), pady=5
        )
        self.locale_combo = ttk.Combobox(
            root,
            textvariable=self.locale_var,
            values=[locale.label for locale in H3_TARGET_LOCALES],
            state="readonly",
        )
        self.locale_combo.grid(row=5, column=1, columnspan=2, sticky="ew", pady=5)

        status = ttk.LabelFrame(root, text="状态", padding=8)
        status.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        status.columnconfigure(1, weight=1)
        self.status_var = tk.StringVar(value="待处理")
        self.progress_var = tk.DoubleVar(value=0)
        ttk.Label(status, textvariable=self.status_var).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Progressbar(
            status,
            maximum=100,
            variable=self.progress_var,
        ).grid(row=0, column=1, sticky="ew", padx=(12, 0))

        ttk.Label(root, text="输出").grid(
            row=7, column=0, sticky="w", padx=(0, 10), pady=8
        )
        self.output_var = tk.StringVar(value="-")
        ttk.Label(root, textvariable=self.output_var).grid(
            row=7, column=1, sticky="w", pady=8
        )
        self.output_button = ttk.Button(
            root,
            text="打开 output",
            command=self._open_output,
        )
        self.output_button.grid(row=7, column=2, sticky="e", pady=8)

        self.error_var = tk.StringVar()
        ttk.Label(
            root,
            textvariable=self.error_var,
            foreground="#b42318",
            wraplength=630,
            justify="left",
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(0, 8))

        self.start_button = ttk.Button(
            root,
            text="开始处理",
            command=self._start,
        )
        self.start_button.grid(row=9, column=0, columnspan=3, pady=(4, 0))

    def _asset_row(
        self,
        root: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        command,
        filetypes: list[tuple[str, str]],
    ) -> ttk.Button:
        ttk.Label(root, text=label).grid(
            row=row, column=0, sticky="w", padx=(0, 10), pady=5
        )
        entry = ttk.Entry(root, textvariable=variable, state="readonly")
        entry.grid(row=row, column=1, sticky="ew", pady=5)
        button = ttk.Button(
            root,
            text="选择",
            command=lambda: command(filetypes),
        )
        button.grid(row=row, column=2, sticky="e", padx=(8, 0), pady=5)
        return button

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

    def _choose_person_image(self, filetypes=None) -> None:
        self._choose_image(self.person_var, "选择人物参考图", filetypes)

    def _choose_scene_image(self, filetypes=None) -> None:
        self._choose_image(self.scene_var, "选择场景参考图", filetypes)

    @staticmethod
    def _choose_image(
        variable: tk.StringVar,
        title: str,
        filetypes: list[tuple[str, str]] | None,
    ) -> None:
        path = filedialog.askopenfilename(
            title=title,
            filetypes=filetypes
            or [
                ("图片", "*.jpg *.jpeg *.png *.webp *.heic *.heif"),
                ("所有文件", "*.*"),
            ],
        )
        if path:
            variable.set(path)

    def _build_spec(self) -> JobSpec:
        if not self.video_var.get().strip():
            raise ValueError("请选择视频")
        locale = locale_from_label(self.locale_var.get())
        if locale is None:
            raise ValueError("请选择目标地区")
        return JobSpec(
            input_video=Path(self.video_var.get().strip()),
            person_image=Path(self.person_var.get().strip())
            if self.person_var.get().strip()
            else None,
            scene_image=Path(self.scene_var.get().strip())
            if self.scene_var.get().strip()
            else None,
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

        self.status_var.set("准备处理")
        self.progress_var.set(0)
        self.output_var.set("-")
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
        self.status_var.set(event.message)
        self.progress_var.set(event.progress)
        if event.error:
            self._set_error(event.error)
        if event.output_path is not None:
            self.output_var.set(f"output/{event.output_path.name}")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.start_button.configure(state=state)
        self.video_choose_button.configure(state=state)
        self.person_choose_button.configure(state=state)
        self.scene_choose_button.configure(state=state)
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
            self._set_error(f"无法打开 output: {exc}")

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
