"""Tkinter desktop window for video localization jobs."""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from config import AppConfig
from core.models import JobSpec, PipelineStage
from core.pipeline import VideoLocalizationPipeline
from language_config import (
    DEFAULT_TARGET_LOCALE_LABEL,
    TARGET_LOCALES,
    locale_from_label,
)
from ui.log_panel import LogPanel
from ui.settings import SettingsPanel


class VideoLocalizerWindow(tk.Tk):
    def __init__(self, config: AppConfig):
        super().__init__()
        self.title("视频入乡随俗工具")
        self.geometry("980x820")
        self.minsize(820, 680)
        self.base_config = config
        self.cancel_event = threading.Event()
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.current_job_id: str | None = None
        self.current_spec: JobSpec | None = None
        self.last_output: Path | None = None
        self.last_error = ""
        self.character_refs: list[Path] = []
        self.scene_refs: list[Path] = []
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_events)

    def _build(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(4, weight=1)

        ttk.Label(root, text="原视频").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        self.input_var = tk.StringVar()
        ttk.Entry(root, textvariable=self.input_var).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(root, text="选择", command=self._choose_video).grid(row=0, column=2, padx=(8, 0))

        ttk.Label(root, text="目标地区").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        self.locale_combo = ttk.Combobox(
            root,
            values=[locale.label for locale in TARGET_LOCALES],
            state="readonly",
        )
        self.locale_combo.grid(row=1, column=1, columnspan=2, sticky="ew", pady=4)
        if DEFAULT_TARGET_LOCALE_LABEL in [locale.label for locale in TARGET_LOCALES]:
            self.locale_combo.set(DEFAULT_TARGET_LOCALE_LABEL)
        else:
            self.locale_combo.current(0)

        refs = ttk.LabelFrame(root, text="参考素材（可选）", padding=8)
        refs.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 8))
        refs.columnconfigure(1, weight=1)
        ttk.Label(refs, text="人物参考图").grid(row=0, column=0, sticky="nw", padx=(0, 8))
        self.character_list = tk.Listbox(refs, height=3, exportselection=False)
        self.character_list.grid(row=0, column=1, sticky="ew")
        ttk.Button(refs, text="添加", command=lambda: self._add_refs("character")).grid(row=0, column=2, padx=4)
        ttk.Button(refs, text="清空", command=lambda: self._clear_refs("character")).grid(row=0, column=3)
        ttk.Label(refs, text="场景参考图").grid(row=1, column=0, sticky="nw", padx=(0, 8), pady=(6, 0))
        self.scene_list = tk.Listbox(refs, height=3, exportselection=False)
        self.scene_list.grid(row=1, column=1, sticky="ew", pady=(6, 0))
        ttk.Button(refs, text="添加", command=lambda: self._add_refs("scene")).grid(row=1, column=2, padx=4, pady=(6, 0))
        ttk.Button(refs, text="清空", command=lambda: self._clear_refs("scene")).grid(row=1, column=3, pady=(6, 0))

        self.settings = SettingsPanel(root, self.base_config)
        self.settings.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(0, 8))

        run_frame = ttk.LabelFrame(root, text="运行状态", padding=8)
        run_frame.grid(row=4, column=0, columnspan=3, sticky="nsew")
        run_frame.columnconfigure(1, weight=1)
        for row, label in enumerate(("当前步骤", "进度", "任务 ID", "请求 ID", "重试次数")):
            ttk.Label(run_frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
        self.stage_var = tk.StringVar(value="待处理")
        self.progress_var = tk.StringVar(value="0%")
        self.task_var = tk.StringVar(value="-")
        self.request_var = tk.StringVar(value="-")
        self.retry_var = tk.StringVar(value="0")
        for row, variable in enumerate((self.stage_var, self.progress_var, self.task_var, self.request_var, self.retry_var)):
            ttk.Label(run_frame, textvariable=variable).grid(row=row, column=1, sticky="w", pady=3)
        self.progress_value = tk.DoubleVar(value=0)
        self.progress = ttk.Progressbar(run_frame, maximum=100, variable=self.progress_value)
        self.progress.grid(row=1, column=2, sticky="ew", padx=(12, 0))
        run_frame.columnconfigure(2, weight=1)

        buttons = ttk.Frame(run_frame)
        buttons.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.start_button = ttk.Button(buttons, text="开始", command=self._start)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(buttons, text="取消", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="打开输出目录", command=self._open_output).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="复制错误信息", command=self._copy_error).pack(side="left", padx=(8, 0))
        self.retry_button = ttk.Button(buttons, text="重新执行失败步骤", command=self._retry, state="disabled")
        self.retry_button.pack(side="left", padx=(8, 0))

        self.log_panel = LogPanel(run_frame)
        self.log_panel.grid(row=6, column=0, columnspan=3, sticky="nsew", pady=(10, 0))
        run_frame.rowconfigure(6, weight=1)

    def _choose_video(self) -> None:
        path = filedialog.askopenfilename(
            title="选择原视频",
            filetypes=[("Video", "*.mp4 *.mov *.mkv *.avi *.webm"), ("All files", "*.*")],
        )
        if path:
            self.input_var.set(path)

    def _add_refs(self, kind: str) -> None:
        paths = filedialog.askopenfilenames(
            title="选择参考图",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.webp"), ("All files", "*.*")],
        )
        if not paths:
            return
        target = self.character_refs if kind == "character" else self.scene_refs
        target.extend(Path(path) for path in paths if Path(path) not in target)
        self._refresh_ref_list(kind)

    def _clear_refs(self, kind: str) -> None:
        if kind == "character":
            self.character_refs.clear()
        else:
            self.scene_refs.clear()
        self._refresh_ref_list(kind)

    def _refresh_ref_list(self, kind: str) -> None:
        widget = self.character_list if kind == "character" else self.scene_list
        paths = self.character_refs if kind == "character" else self.scene_refs
        widget.delete(0, "end")
        for path in paths:
            widget.insert("end", str(path))

    def _build_spec(self) -> JobSpec:
        input_path = Path(self.input_var.get().strip()).expanduser()
        if not input_path.is_file():
            raise ValueError("请选择存在的原视频文件")
        target_locale = locale_from_label(self.locale_combo.get())
        if target_locale is None:
            raise ValueError("请选择目标地区")
        return JobSpec(
            input_video=input_path,
            target_language=target_locale.language_code,
            target_region=target_locale.region,
            target_locale=target_locale.locale_code,
            character_refs=list(self.character_refs),
            scene_refs=list(self.scene_refs),
        )

    def _effective_config(self) -> AppConfig:
        overrides = self.settings.get_overrides()
        return self.base_config.with_overrides(**overrides)

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            spec = self._build_spec()
            config = self._effective_config()
            self.settings.save()
        except Exception as exc:  # noqa: BLE001 - GUI validation message
            messagebox.showerror("输入错误", str(exc))
            return
        self.current_spec = spec
        self.current_job_id = None
        self.last_output = None
        self.last_error = ""
        self._run_in_background(config, spec)

    def _run_in_background(self, config: AppConfig, spec: JobSpec, *, retry: bool = False) -> None:
        self.cancel_event = threading.Event()
        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.retry_button.configure(state="disabled")

        def worker() -> None:
            pipeline = VideoLocalizationPipeline(
                config,
                event_callback=self.events.put,
                cancel_event=self.cancel_event,
            )
            try:
                if retry and self.current_job_id:
                    pipeline.resume_failed(self.current_job_id, spec=spec)
                else:
                    pipeline.run(spec)
            except Exception as exc:  # noqa: BLE001 - pipeline already logs detail
                self.events.put({
                    "event_type": "error",
                    "job_id": self.current_job_id or "",
                    "stage": PipelineStage.FAILED.value,
                    "progress": 0,
                    "message": str(exc),
                    "metadata": {},
                })

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _handle_event(self, event: dict[str, Any]) -> None:
        if event.get("job_id"):
            self.current_job_id = str(event["job_id"])
        stage = str(event.get("stage", ""))
        stage_labels = {
            PipelineStage.ANALYZING.value: "分析视频",
            PipelineStage.GENERATING_VIDEO.value: "生成本地化视频（含声音与口型）",
            PipelineStage.COMPLETED.value: "已完成",
            PipelineStage.FAILED.value: "失败",
        }
        progress = int(event.get("progress", 0) or 0)
        if stage:
            self.stage_var.set(stage_labels.get(stage, stage))
        self.progress_var.set(f"{progress}%")
        self.progress_value.set(progress)
        event_type = event.get("event_type")
        if event_type == "log":
            self.log_panel.append(str(event.get("message", "")))
            attempt = event.get("metadata", {}).get("attempt")
            if attempt is not None:
                self.retry_var.set(str(attempt))
        elif event_type == "task":
            metadata = event.get("metadata", {})
            if metadata.get("task_id"):
                self.task_var.set(str(metadata["task_id"]))
            if metadata.get("request_id"):
                self.request_var.set(str(metadata["request_id"]))
        elif event_type == "error":
            self.last_error = str(event.get("message", ""))
            self.log_panel.append(f"ERROR: {self.last_error}")
            self.retry_button.configure(state="normal")
        elif event_type == "completed":
            output = event.get("metadata", {}).get("output") or event.get("message")
            if output:
                self.last_output = Path(str(output))
                self.log_panel.append(f"输出：{output}")
        if event_type in {"completed", "error"}:
            self.start_button.configure(state="normal")
            self.cancel_button.configure(state="disabled")

    def _cancel(self) -> None:
        self.cancel_event.set()
        self.cancel_button.configure(state="disabled")
        self.log_panel.append("已请求取消，当前外部调用结束后停止后续阶段。")

    def _retry(self) -> None:
        if not self.current_job_id or not self.current_spec:
            return
        try:
            config = self._effective_config()
            self.settings.save()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("配置错误", str(exc))
            return
        self._run_in_background(config, self.current_spec, retry=True)

    def _on_close(self) -> None:
        """Persist settings before closing; ask a running worker to stop cooperatively."""

        try:
            self.settings.save()
        except OSError as exc:
            messagebox.showwarning("设置保存失败", f"本次设置未能保存：{exc}")
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
        self.destroy()

    def _open_output(self) -> None:
        target = self.last_output.parent if self.last_output else self.base_config.work_dir
        target.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(target))  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("无法打开目录", str(exc))

    def _copy_error(self) -> None:
        if not self.last_error:
            return
        self.clipboard_clear()
        self.clipboard_append(self.last_error)
        self.update()


def run_gui(config: AppConfig) -> None:
    window = VideoLocalizerWindow(config)
    window.mainloop()
