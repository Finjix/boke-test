"""Tkinter desktop window for the MiniMax H3-Context-IR workflow."""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from config import AppConfig
from core.models import JobSpec, PipelineStage
from core.pipeline import VideoLocalizationPipeline
from language_config import (
    DEFAULT_TARGET_LOCALE_LABEL,
    H3_TARGET_LOCALES,
    locale_from_label,
)
from ui.settings import SettingsPanel


class VideoLocalizerWindow(tk.Tk):
    """A single current-task view with no history or recovery controls."""

    def __init__(self, config: AppConfig):
        super().__init__()
        self.title("MiniMax H3-Context-IR 视频本地化工具")
        self.geometry("980x760")
        self.minsize(820, 620)
        self.base_config = config
        self.cancel_event = threading.Event()
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.pipeline: VideoLocalizationPipeline | None = None
        self.current_job_id: str | None = None
        self.current_spec: JobSpec | None = None
        self.last_output: Path | None = None
        self.last_error = ""
        self._busy = False
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_events)

    def _build(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)

        current = ttk.Frame(root, padding=4)
        current.grid(row=0, column=0, sticky="nsew")
        current.columnconfigure(1, weight=1)
        current.rowconfigure(3, weight=1)
        self._build_current_tab(current)

    def _build_current_tab(self, root: ttk.Frame) -> None:
        ttk.Label(root, text="源视频").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=4
        )
        self.input_var = tk.StringVar()
        ttk.Entry(root, textvariable=self.input_var).grid(
            row=0, column=1, sticky="ew", pady=4
        )
        ttk.Button(root, text="选择", command=self._choose_video).grid(
            row=0, column=2, padx=(8, 0)
        )

        ttk.Label(root, text="目标地区").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=4
        )
        locale_values = [locale.label for locale in H3_TARGET_LOCALES]
        self.locale_combo = ttk.Combobox(
            root, values=locale_values, state="readonly"
        )
        self.locale_combo.grid(row=1, column=1, columnspan=2, sticky="ew", pady=4)
        if DEFAULT_TARGET_LOCALE_LABEL in locale_values:
            self.locale_combo.set(DEFAULT_TARGET_LOCALE_LABEL)
        elif locale_values:
            self.locale_combo.current(0)

        self.settings = SettingsPanel(root, self.base_config)
        self.settings.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, 8))

        status = ttk.LabelFrame(root, text="当前任务", padding=8)
        status.grid(row=3, column=0, columnspan=3, sticky="nsew")
        status.columnconfigure(1, weight=1)
        status.rowconfigure(8, weight=1)
        labels = (
            "当前阶段",
            "进度",
            "当前片段",
            "IR task ID",
            "H3 task ID",
            "输出",
            "错误",
        )
        for row, label in enumerate(labels):
            ttk.Label(status, text=label).grid(
                row=row, column=0, sticky="w", padx=(0, 8), pady=3
            )

        self.stage_var = tk.StringVar(value="待处理")
        self.progress_var = tk.StringVar(value="0%")
        self.segment_var = tk.StringVar(value="-")
        self.ir_task_var = tk.StringVar(value="-")
        self.h3_task_var = tk.StringVar(value="-")
        self.output_var = tk.StringVar(value="-")
        self.error_var = tk.StringVar(value="-")
        variables = (
            self.stage_var,
            self.progress_var,
            self.segment_var,
            self.ir_task_var,
            self.h3_task_var,
            self.output_var,
            self.error_var,
        )
        for row, variable in enumerate(variables):
            ttk.Label(status, textvariable=variable).grid(
                row=row, column=1, columnspan=2, sticky="w", pady=3
            )
        self.progress_value = tk.DoubleVar(value=0)
        ttk.Progressbar(status, maximum=100, variable=self.progress_value).grid(
            row=1, column=2, sticky="ew", padx=(12, 0)
        )
        status.columnconfigure(2, weight=1)

        buttons = ttk.Frame(status)
        buttons.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.start_button = ttk.Button(buttons, text="开始处理", command=self._start)
        self.start_button.pack(side="left")
        ttk.Button(buttons, text="打开输出目录", command=self._open_output).pack(
            side="left", padx=(8, 0)
        )

        prompt_frame = ttk.LabelFrame(
            status, text="IR 增强提示词", padding=6
        )
        prompt_frame.grid(row=8, column=0, columnspan=3, sticky="nsew", pady=(10, 0))
        prompt_frame.columnconfigure(0, weight=1)
        prompt_frame.rowconfigure(0, weight=1)
        self.prompt_text = tk.Text(
            prompt_frame, height=10, state="disabled", wrap="word"
        )
        prompt_scrollbar = ttk.Scrollbar(
            prompt_frame, orient="vertical", command=self.prompt_text.yview
        )
        self.prompt_text.configure(yscrollcommand=prompt_scrollbar.set)
        self.prompt_text.grid(row=0, column=0, sticky="nsew")
        prompt_scrollbar.grid(row=0, column=1, sticky="ns")

    def _choose_video(self) -> None:
        path = filedialog.askopenfilename(
            title="选择源视频",
            filetypes=[("Video", "*.mp4 *.mov *.mkv *.avi *.webm"), ("All files", "*.*")],
        )
        if path:
            self.input_var.set(path)

    def _build_spec(self) -> JobSpec:
        input_path = Path(self.input_var.get().strip()).expanduser()
        if not input_path.is_file():
            raise ValueError("请选择存在的源视频文件")
        target_locale = locale_from_label(self.locale_combo.get())
        if target_locale is None:
            raise ValueError("请选择目标地区")
        return JobSpec(input_video=input_path, target_locale=target_locale.locale_code)

    def _effective_config(self) -> AppConfig:
        return self.base_config.with_overrides(
            **self.settings.get_non_empty_overrides()
        )

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            spec = self._build_spec()
            config = self._effective_config()
            self.settings.save()
            if not config.minimax_api_key.strip():
                raise ValueError("请在 MiniMax 设置中填写 API Key")
        except Exception as exc:  # noqa: BLE001 - GUI validation message
            messagebox.showerror("输入错误", str(exc))
            return

        self.pipeline = VideoLocalizationPipeline(
            config,
            event_callback=self.events.put,
            cancel_event=self.cancel_event,
        )
        self.current_spec = spec
        self.current_job_id = None
        self.last_output = None
        self.last_error = ""
        self._set_prompt("")
        self._reset_status()
        self._run_in_background("new", spec=spec)

    def _append_segment(self) -> None:
        if self.pipeline is None or self.pipeline.job is None:
            messagebox.showinfo("尚未开始", "请先开始一个长视频任务。")
            return
        path = filedialog.askopenfilename(
            title="选择下一片（3–15 秒，必须按顺序）",
            filetypes=[("Video", "*.mp4 *.mov *.mkv *.avi *.webm"), ("All files", "*.*")],
        )
        if path:
            self._run_in_background("append", video_path=Path(path))

    def _finish(self) -> None:
        self._run_in_background("finish")

    def _run_in_background(
        self,
        operation: str,
        *,
        spec: JobSpec | None = None,
        video_path: Path | None = None,
    ) -> None:
        if self.worker and self.worker.is_alive():
            return
        if self.pipeline is None:
            return
        self._busy = True
        self._set_action_buttons_busy(True)
        pipeline = self.pipeline

        def worker() -> None:
            try:
                if operation == "new":
                    if spec is None:
                        raise ValueError("新任务缺少 JobSpec")
                    pipeline.run(spec)
                elif operation == "append":
                    if video_path is None:
                        raise ValueError("下一片视频路径为空")
                    pipeline.append_segment(video_path)
                elif operation == "finish":
                    pipeline.finalize()
                else:
                    raise ValueError(f"未知操作：{operation}")
            except Exception as exc:  # noqa: BLE001 - show terminal task error
                if pipeline.job is None or pipeline.job.stage != PipelineStage.FAILED:
                    self.events.put(
                        {
                            "event_type": "error",
                            "job_id": pipeline.job.job_id if pipeline.job else "",
                            "stage": PipelineStage.FAILED.value,
                            "progress": 0,
                            "message": str(exc),
                            "metadata": {},
                        }
                    )
            finally:
                self.events.put(
                    {
                        "event_type": "worker_finished",
                        "job_id": pipeline.job.job_id if pipeline.job else "",
                        "stage": pipeline.job.stage.value if pipeline.job else "",
                        "progress": pipeline.job.progress if pipeline.job else 0,
                        "message": "",
                        "metadata": {},
                    }
                )

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _poll_events(self) -> None:
        try:
            while True:
                self._handle_event(self.events.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _handle_event(self, event: dict[str, Any]) -> None:
        if event.get("job_id"):
            self.current_job_id = str(event["job_id"])
        event_type = event.get("event_type")
        if event_type == "worker_finished":
            self._finish_worker_state()
            return

        stage = str(event.get("stage", ""))
        stage_labels = {
            PipelineStage.PREPARING.value: "准备任务",
            PipelineStage.WAITING_FOR_SEGMENTS.value: "等待上传视频片段",
            PipelineStage.GENERATING_CONTEXT_IR.value: "生成 H3-Context-IR 增强提示词",
            PipelineStage.WAITING_FOR_CONTEXT_IR.value: "等待 H3-Context-IR",
            PipelineStage.GENERATING_VIDEO.value: "生成 H3 视频",
            PipelineStage.WAITING_FOR_VIDEO.value: "等待 H3 视频",
            PipelineStage.WAITING_FOR_NEXT_SEGMENT.value: "等待下一片或完成拼接",
            PipelineStage.COMPLETED.value: "已完成",
            PipelineStage.FAILED.value: "失败",
        }
        if stage:
            self.stage_var.set(stage_labels.get(stage, stage))
        progress = int(event.get("progress", 0) or 0)
        self.progress_var.set(f"{progress}%")
        self.progress_value.set(progress)
        metadata = event.get("metadata", {}) or {}
        if metadata.get("segment_index") is not None:
            self.segment_var.set(str(metadata["segment_index"]))

        if event_type == "task":
            task_id = metadata.get("task_id")
            provider = str(metadata.get("provider", ""))
            if task_id and provider == "minimax_context_ir":
                self.ir_task_var.set(str(task_id))
            elif task_id and provider == "minimax_h3":
                self.h3_task_var.set(str(task_id))
        elif event_type == "prompt_ready":
            self._set_prompt(str(metadata.get("prompt", "")))
        elif event_type == "error":
            self.last_error = str(event.get("message", ""))
            self.error_var.set(self.last_error or "-")
        elif event_type in {"segment_completed", "completed"}:
            output = metadata.get("output")
            if output:
                self.last_output = Path(str(output))
                self.output_var.set(str(output))
        self._refresh_current_actions()

    def _set_action_buttons_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.start_button.configure(state=state)

    def _finish_worker_state(self) -> None:
        self._busy = False
        self._set_action_buttons_busy(False)

    def _refresh_current_actions(self) -> None:
        # The current-task UI intentionally exposes no continuation controls.
        return

    def _reset_status(self) -> None:
        self.stage_var.set("待处理")
        self.progress_var.set("0%")
        self.progress_value.set(0)
        self.segment_var.set("-")
        self.ir_task_var.set("-")
        self.h3_task_var.set("-")
        self.output_var.set("-")
        self.error_var.set("-")

    def _set_prompt(self, text: str) -> None:
        self.prompt_text.configure(state="normal")
        self.prompt_text.delete("1.0", "end")
        if text:
            self.prompt_text.insert("1.0", text)
        self.prompt_text.configure(state="disabled")

    def _on_close(self) -> None:
        try:
            self.settings.save()
        except OSError as exc:
            messagebox.showwarning("设置保存失败", f"本次设置未能保存：{exc}")
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
        self.destroy()

    def _open_output(self) -> None:
        target = self.last_output.parent if self.last_output else self.base_config.work_dir
        self._open_path(target)

    @staticmethod
    def _open_path(target: Path) -> None:
        target = Path(target)
        target.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(target))  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("无法打开目录", str(exc))


def run_gui(config: AppConfig) -> None:
    window = VideoLocalizerWindow(config)
    window.mainloop()
