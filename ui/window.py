"""Tkinter desktop window for the MiniMax H3 workflow."""

from __future__ import annotations

import json
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from config import AppConfig
from core.models import JobSpec, NodeExecutionStatus, PipelineStage
from core.pipeline import VideoLocalizationPipeline
from language_config import (
    DEFAULT_TARGET_LOCALE_LABEL,
    H3_TARGET_LOCALES,
    locale_from_label,
)
from ui.history import HistoryPanel
from ui.log_panel import LogPanel
from ui.settings import SettingsPanel
from utils.history import HistoryStore


class VideoLocalizerWindow(tk.Tk):
    def __init__(self, config: AppConfig):
        super().__init__()
        self.title("MiniMax H3 视频转化工具")
        self.geometry("1180x860")
        self.minsize(900, 700)
        self.base_config = config
        self.history_store = HistoryStore(config.work_dir)
        self.cancel_event = threading.Event()
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.current_job_id: str | None = None
        self.current_spec: JobSpec | None = None
        self.last_output: Path | None = None
        self.last_error = ""
        self.reference_images: list[Path] = []
        self._busy = False
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_events)

    def _build(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)

        self.notebook = ttk.Notebook(root)
        self.notebook.grid(row=0, column=0, sticky="nsew")
        current_tab = ttk.Frame(self.notebook, padding=4)
        self.notebook.add(current_tab, text="当前任务")
        self.history_panel = HistoryPanel(
            self.notebook,
            self.history_store,
            action_callback=self._history_action,
            open_path_callback=self._open_path,
        )
        self.notebook.add(self.history_panel, text="执行历史")
        self._build_current_tab(current_tab)

    def _build_current_tab(self, root: ttk.Frame) -> None:
        root.columnconfigure(1, weight=1)
        root.rowconfigure(5, weight=1)

        ttk.Label(root, text="原视频 / 主参考").grid(
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
        self.locale_combo = ttk.Combobox(root, values=locale_values, state="readonly")
        self.locale_combo.grid(row=1, column=1, columnspan=2, sticky="ew", pady=4)
        if DEFAULT_TARGET_LOCALE_LABEL in locale_values:
            self.locale_combo.set(DEFAULT_TARGET_LOCALE_LABEL)
        elif locale_values:
            self.locale_combo.current(0)

        ttk.Label(root, text="转化要求").grid(
            row=2, column=0, sticky="nw", padx=(0, 8), pady=4
        )
        self.instruction_text = tk.Text(root, height=3, wrap="word")
        self.instruction_text.insert(
            "1.0",
            "将人物和场景转化为目标地区版本，严格保持人物、场景、创意结构、镜头节奏和整体效果一致。",
        )
        self.instruction_text.grid(row=2, column=1, columnspan=2, sticky="ew", pady=4)

        refs = ttk.LabelFrame(root, text="风格参考图（可选，最多 9 张；不上传参考视频）", padding=8)
        refs.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 8))
        refs.columnconfigure(0, weight=1)
        self.reference_list = tk.Listbox(refs, height=4, exportselection=False)
        self.reference_list.grid(row=0, column=0, rowspan=2, sticky="ew")
        ttk.Button(refs, text="添加图片", command=self._add_reference_images).grid(
            row=0, column=1, padx=(8, 0), pady=2
        )
        ttk.Button(refs, text="清空", command=self._clear_reference_images).grid(
            row=1, column=1, padx=(8, 0), pady=2
        )

        self.settings = SettingsPanel(root, self.base_config)
        self.settings.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(0, 8))

        run_frame = ttk.LabelFrame(root, text="运行状态", padding=8)
        run_frame.grid(row=5, column=0, columnspan=3, sticky="nsew")
        run_frame.columnconfigure(1, weight=1)
        run_frame.rowconfigure(8, weight=1)
        for row, label in enumerate(("当前步骤", "进度", "H3 task ID", "请求 ID", "片段 / 尝试")):
            ttk.Label(run_frame, text=label).grid(
                row=row, column=0, sticky="w", padx=(0, 8), pady=3
            )
        self.stage_var = tk.StringVar(value="待处理")
        self.progress_var = tk.StringVar(value="0%")
        self.task_var = tk.StringVar(value="-")
        self.request_var = tk.StringVar(value="-")
        self.retry_var = tk.StringVar(value="0")
        for row, variable in enumerate(
            (self.stage_var, self.progress_var, self.task_var, self.request_var, self.retry_var)
        ):
            ttk.Label(run_frame, textvariable=variable).grid(row=row, column=1, sticky="w", pady=3)
        self.progress_value = tk.DoubleVar(value=0)
        ttk.Progressbar(run_frame, maximum=100, variable=self.progress_value).grid(
            row=1, column=2, sticky="ew", padx=(12, 0)
        )
        run_frame.columnconfigure(2, weight=1)

        buttons = ttk.Frame(run_frame)
        buttons.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.start_button = ttk.Button(buttons, text="开始 H3 转化", command=self._start)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(buttons, text="取消等待", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=(8, 0))
        self.append_button = ttk.Button(
            buttons, text="上传下一片", command=self._append_segment, state="disabled"
        )
        self.append_button.pack(side="left", padx=(8, 0))
        self.continue_button = ttk.Button(
            buttons, text="继续等待 H3", command=self._continue_h3, state="disabled"
        )
        self.continue_button.pack(side="left", padx=(8, 0))
        self.retry_button = ttk.Button(
            buttons, text="重试当前片段", command=self._retry, state="disabled"
        )
        self.retry_button.pack(side="left", padx=(8, 0))
        self.finish_button = ttk.Button(
            buttons, text="拼接已完成片段", command=self._finish, state="disabled"
        )
        self.finish_button.pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="打开输出目录", command=self._open_output).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(buttons, text="复制错误信息", command=self._copy_error).pack(
            side="left", padx=(8, 0)
        )

        self.review_frame = ttk.LabelFrame(run_frame, text="H3 节点结果（只读）", padding=6)
        self.review_frame.grid(row=6, column=0, columnspan=3, sticky="nsew", pady=(10, 0))
        self.review_frame.columnconfigure(0, weight=1)
        self.review_frame.rowconfigure(0, weight=1)
        self.review_text = tk.Text(self.review_frame, height=10, state="disabled", wrap="word")
        review_scrollbar = ttk.Scrollbar(
            self.review_frame, orient="vertical", command=self.review_text.yview
        )
        self.review_text.configure(yscrollcommand=review_scrollbar.set)
        self.review_text.grid(row=0, column=0, sticky="nsew")
        review_scrollbar.grid(row=0, column=1, sticky="ns")

        self.log_panel = LogPanel(run_frame)
        self.log_panel.grid(row=8, column=0, columnspan=3, sticky="nsew", pady=(10, 0))

    def _choose_video(self) -> None:
        path = filedialog.askopenfilename(
            title="选择原视频 / 主参考",
            filetypes=[("Video", "*.mp4 *.mov *.mkv *.avi *.webm"), ("All files", "*.*")],
        )
        if path:
            self.input_var.set(path)

    def _add_reference_images(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择 H3 风格参考图（最多 9 张）",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.webp *.heic *.heif"), ("All files", "*.*")],
        )
        if not paths:
            return
        for raw in paths:
            path = Path(raw)
            if path not in self.reference_images:
                self.reference_images.append(path)
        if len(self.reference_images) > 9:
            self.reference_images = self.reference_images[:9]
            messagebox.showwarning("参考图数量", "H3 最多接受 9 张参考图，超出的图片未加入。")
        self._refresh_reference_list()

    def _clear_reference_images(self) -> None:
        self.reference_images.clear()
        self._refresh_reference_list()

    def _refresh_reference_list(self) -> None:
        self.reference_list.delete(0, "end")
        for path in self.reference_images:
            self.reference_list.insert("end", str(path))

    def _build_spec(self) -> JobSpec:
        input_path = Path(self.input_var.get().strip()).expanduser()
        if not input_path.is_file():
            raise ValueError("请选择存在的原视频文件")
        target_locale = locale_from_label(self.locale_combo.get())
        if target_locale is None:
            raise ValueError("请选择 H3 支持的目标地区")
        instruction = self.instruction_text.get("1.0", "end").strip()
        return JobSpec(
            input_video=input_path,
            target_language=target_locale.language_code,
            target_region=target_locale.region,
            target_locale=target_locale.locale_code,
            reference_images=list(self.reference_images),
            transformation_instruction=instruction,
        )

    def _effective_config(self) -> AppConfig:
        return self.base_config.with_overrides(**self.settings.get_overrides())

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
        self._set_review("")
        self._run_in_background(config, spec, operation="new")

    def _append_segment(self) -> None:
        if not self.current_job_id:
            messagebox.showinfo("尚未开始", "请先开始一个超过 15 秒的主任务。")
            return
        path = filedialog.askopenfilename(
            title="选择下一片（4–15 秒，必须按顺序）",
            filetypes=[("Video", "*.mp4 *.mov *.mkv *.avi *.webm"), ("All files", "*.*")],
        )
        if path:
            self._run_in_background(
                self._effective_config(),
                operation="append",
                job_id=self.current_job_id,
                video_path=Path(path),
            )

    def _continue_h3(self) -> None:
        if self.current_job_id:
            self._run_in_background(
                self._effective_config(), operation="continue", job_id=self.current_job_id
            )

    def _retry(self) -> None:
        if self.current_job_id:
            self._run_in_background(
                self._effective_config(), operation="retry", job_id=self.current_job_id
            )

    def _finish(self) -> None:
        if self.current_job_id:
            self._run_in_background(
                self._effective_config(), operation="finish", job_id=self.current_job_id
            )

    def _run_in_background(
        self,
        config: AppConfig,
        spec: JobSpec | None = None,
        *,
        operation: str,
        job_id: str | None = None,
        video_path: Path | None = None,
    ) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.cancel_event = threading.Event()
        self._busy = True
        self._set_action_buttons_busy(True)
        self.history_panel.set_busy(True)
        operation_job_id = job_id or self.current_job_id

        def worker() -> None:
            pipeline = VideoLocalizationPipeline(
                config,
                event_callback=self.events.put,
                cancel_event=self.cancel_event,
                history_store=self.history_store,
            )
            try:
                if operation == "new":
                    if spec is None:
                        raise ValueError("新任务缺少 JobSpec")
                    pipeline.run(spec, execution_mode="auto")
                elif not operation_job_id:
                    raise ValueError("历史任务缺少 job_id")
                elif operation == "append":
                    if video_path is None:
                        raise ValueError("下一片视频路径为空")
                    pipeline.append_segment(operation_job_id, video_path)
                elif operation == "continue":
                    pipeline.continue_segment(operation_job_id)
                elif operation == "retry":
                    pipeline.retry_segment(operation_job_id)
                elif operation == "finish":
                    pipeline.finalize(operation_job_id)
                else:
                    raise ValueError(f"未知操作：{operation}")
            except Exception as exc:  # noqa: BLE001 - pipeline persists details
                self.events.put(
                    {
                        "event_type": "error",
                        "job_id": operation_job_id or self.current_job_id or "",
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
                        "job_id": operation_job_id or self.current_job_id or "",
                        "stage": "",
                        "progress": 0,
                        "message": "",
                        "metadata": {},
                    }
                )

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
        event_type = event.get("event_type")
        if event_type == "worker_finished":
            self._finish_worker_state()
            return
        stage = str(event.get("stage", ""))
        stage_labels = {
            PipelineStage.PREPARING.value: "准备任务",
            PipelineStage.WAITING_FOR_SEGMENTS.value: "等待上传视频片段",
            PipelineStage.GENERATING_SEGMENT.value: "H3 生成片段",
            PipelineStage.WAITING_FOR_NEXT_SEGMENT.value: "等待下一片",
            PipelineStage.COMPLETED.value: "已完成",
            PipelineStage.FAILED.value: "失败（可重试）",
        }
        if stage:
            self.stage_var.set(stage_labels.get(stage, stage))
        progress = int(event.get("progress", 0) or 0)
        self.progress_var.set(f"{progress}%")
        self.progress_value.set(progress)
        metadata = event.get("metadata", {}) or {}
        if event_type == "log":
            self.log_panel.append(str(event.get("message", "")))
        elif event_type == "task":
            if metadata.get("task_id"):
                self.task_var.set(str(metadata["task_id"]))
            if metadata.get("request_id"):
                self.request_var.set(str(metadata["request_id"]))
            if metadata.get("segment_index") is not None:
                self.retry_var.set(
                    f"{metadata.get('segment_index')} / {metadata.get('attempt', '-') }"
                )
        elif event_type == "segments_required":
            self.log_panel.append(str(event.get("message", "请按顺序上传 4–15 秒片段。")))
        elif event_type == "error":
            self.last_error = str(event.get("message", ""))
            self.log_panel.append(f"ERROR: {self.last_error}")
        elif event_type == "completed":
            output = metadata.get("output")
            if output:
                self.last_output = Path(str(output))
                self.log_panel.append(f"输出：{output}")
        if event_type in {
            "completed",
            "error",
            "segments_required",
            "node_completed",
            "node_failed",
        }:
            self.history_panel.refresh()
            self._refresh_current_actions()

    def _set_action_buttons_busy(self, busy: bool) -> None:
        if busy:
            self.start_button.configure(state="disabled")
            self.cancel_button.configure(state="normal")
            for button in (
                self.append_button,
                self.continue_button,
                self.retry_button,
                self.finish_button,
            ):
                button.configure(state="disabled")
        else:
            self.start_button.configure(state="normal")
            self.cancel_button.configure(state="disabled")
            self._refresh_current_actions()

    def _finish_worker_state(self) -> None:
        self._busy = False
        self._set_action_buttons_busy(False)
        self.history_panel.set_busy(False)

    def _refresh_current_actions(self) -> None:
        buttons = (
            self.append_button,
            self.continue_button,
            self.retry_button,
            self.finish_button,
        )
        for button in buttons:
            button.configure(state="disabled")
        if self._busy or not self.current_job_id:
            return
        try:
            context = self.history_store.load_context(self.current_job_id)
        except Exception:
            return
        if context.stage in {
            PipelineStage.WAITING_FOR_SEGMENTS,
            PipelineStage.WAITING_FOR_NEXT_SEGMENT,
        }:
            self.append_button.configure(state="normal")
        segment = next(
            (item for item in context.h3_segments if item.status != "completed"), None
        )
        if segment is not None:
            active = next(
                (
                    attempt
                    for attempt in segment.attempts
                    if attempt.attempt == segment.active_attempt
                    and attempt.status == NodeExecutionStatus.RUNNING
                ),
                None,
            )
            if active and active.task_id:
                self.continue_button.configure(state="normal")
            elif segment.attempts and segment.attempts[-1].status == NodeExecutionStatus.FAILED:
                self.retry_button.configure(state="normal")
        if (
            len(context.h3_segments) > 0
            and context.stage == PipelineStage.WAITING_FOR_NEXT_SEGMENT
            and all(item.status == "completed" for item in context.h3_segments)
        ):
            self.finish_button.configure(state="normal")
        if context.stage == PipelineStage.COMPLETED:
            self.last_output = self._artifact_path(context, "final_video")
        self._show_context(context)

    def _history_action(self, job_id: str, action: str) -> None:
        try:
            context = self.history_store.load_context(job_id)
        except Exception as exc:
            messagebox.showerror("无法打开历史任务", str(exc))
            return
        self.current_job_id = job_id
        self.current_spec = context.spec
        self.last_output = self._artifact_path(context, "final_video")
        self.notebook.select(0)
        if action == "append":
            self._append_segment()
        else:
            self._run_in_background(self._effective_config(), operation=action, job_id=job_id)

    def _show_context(self, context: Any) -> None:
        try:
            self._set_review(json.dumps(context.model_dump(mode="json"), ensure_ascii=False, indent=2))
        except Exception:
            pass

    def _set_review(self, text: str) -> None:
        self.review_text.configure(state="normal")
        self.review_text.delete("1.0", "end")
        if text:
            self.review_text.insert("1.0", text)
        self.review_text.configure(state="disabled")

    @staticmethod
    def _artifact_path(context: Any, name: str) -> Path | None:
        value = context.artifacts.get(name)
        if not value:
            return None
        path = Path(value)
        return path if path.is_absolute() else context.job_dir / path

    def _cancel(self) -> None:
        self.cancel_event.set()
        self.cancel_button.configure(state="disabled")
        self.log_panel.append("已请求取消；若 H3 task 已创建，可稍后使用“继续等待 H3”。")

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

    def _copy_error(self) -> None:
        if not self.last_error:
            return
        self.clipboard_clear()
        self.clipboard_append(self.last_error)
        self.update()


def run_gui(config: AppConfig) -> None:
    window = VideoLocalizerWindow(config)
    window.mainloop()
