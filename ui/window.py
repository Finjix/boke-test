"""Tkinter desktop window for the Doubao + MiniMax H3 workflow."""

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
from core.models import ApprovalStatus, JobSpec, NodeExecutionStatus, PipelineStage
from core.pipeline import VideoLocalizationPipeline
from language_config import (
    DEFAULT_TARGET_LOCALE_LABEL,
    H3_TARGET_LOCALES,
    locale_from_label,
)
from media.images import inspect_image
from ui.history import HistoryPanel
from ui.log_panel import LogPanel
from ui.settings import SettingsPanel
from utils.history import HistoryStore


class VideoLocalizerWindow(tk.Tk):
    def __init__(self, config: AppConfig):
        super().__init__()
        self.title("Doubao Seed + MiniMax H3 视频转化工具")
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

        refs = ttk.LabelFrame(root, text="分镜参考图", padding=8)
        refs.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 8))
        ttk.Label(
            refs,
            text=(
                "前端不上传用户参考图。Doubao 会分析完整源视频并为每个镜头选择关键帧，"
                "随后由 Seedream 生成低成本场景参考图；生成结果会在第二次确认前留存。"
            ),
            wraplength=920,
            justify="left",
        ).grid(row=0, column=0, sticky="w")

        self.settings = SettingsPanel(root, self.base_config)
        self.settings.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(0, 8))

        run_frame = ttk.LabelFrame(root, text="运行状态", padding=8)
        run_frame.grid(row=5, column=0, columnspan=3, sticky="nsew")
        run_frame.columnconfigure(1, weight=1)
        run_frame.rowconfigure(8, weight=1)
        for row, label in enumerate(
            ("当前步骤", "进度", "H3 task ID", "最近请求 ID", "片段 / 尝试")
        ):
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
        self.start_button = ttk.Button(buttons, text="开始 Doubao + H3 转化", command=self._start)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(buttons, text="取消等待", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=(8, 0))
        self.approve_button = ttk.Button(
            buttons,
            text="确认 Doubao，生成参考图",
            command=self._approve_doubao,
            state="disabled",
        )
        self.approve_button.pack(side="left", padx=(8, 0))
        self.approve_seedream_button = ttk.Button(
            buttons,
            text="确认参考图并进入 H3",
            command=self._approve_seedream,
            state="disabled",
        )
        self.approve_seedream_button.pack(side="left", padx=(8, 0))
        self.seedream_retry_button = ttk.Button(
            buttons,
            text="重试 Seedream",
            command=self._retry_seedream,
            state="disabled",
        )
        self.seedream_retry_button.pack(side="left", padx=(8, 0))
        self.analysis_retry_button = ttk.Button(
            buttons,
            text="重试 Doubao",
            command=self._retry_doubao,
            state="disabled",
        )
        self.analysis_retry_button.pack(side="left", padx=(8, 0))
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

        self.review_frame = ttk.LabelFrame(
            run_frame,
            text="Doubao 分析与 H3 节点结果（只读）",
            padding=6,
        )
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

    def _approve_doubao(self) -> None:
        if self.current_job_id:
            self._run_in_background(
                self._effective_config(),
                operation="approve_doubao",
                job_id=self.current_job_id,
            )

    def _retry_doubao(self) -> None:
        if self.current_job_id:
            self._run_in_background(
                self._effective_config(),
                operation="retry_doubao",
                job_id=self.current_job_id,
            )

    def _approve_seedream(self) -> None:
        if self.current_job_id:
            self._run_in_background(
                self._effective_config(),
                operation="approve_seedream",
                job_id=self.current_job_id,
            )

    def _retry_seedream(self) -> None:
        if self.current_job_id:
            self._run_in_background(
                self._effective_config(),
                operation="retry_seedream",
                job_id=self.current_job_id,
            )

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
                    pipeline.run(
                        spec,
                        execution_mode=(
                            "auto"
                            if self.settings.get_auto_continue_to_h3()
                            else "manual"
                        ),
                    )
                elif not operation_job_id:
                    raise ValueError("历史任务缺少 job_id")
                elif operation == "approve_doubao":
                    pipeline.approve_doubao(operation_job_id)
                elif operation == "approve_seedream":
                    pipeline.approve_seedream(operation_job_id)
                elif operation == "retry_doubao":
                    pipeline.retry_doubao(operation_job_id)
                elif operation == "retry_seedream":
                    pipeline.retry_seedream(operation_job_id)
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
            PipelineStage.ANALYZING.value: "Doubao 分析原视频",
            PipelineStage.WAITING_FOR_APPROVAL.value: "等待确认 Doubao 方案",
            PipelineStage.GENERATING_REFERENCES.value: "Seedream 生成分镜参考图",
            PipelineStage.WAITING_FOR_REFERENCE_APPROVAL.value: "等待确认分镜参考图",
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
        elif event_type == "provider_call":
            if metadata.get("request_id"):
                self.request_var.set(str(metadata["request_id"]))
        elif event_type == "approval_required":
            self.log_panel.append(
                "Doubao 分析已保存，请确认后生成 Seedream 分镜参考图。"
            )
        elif event_type == "reference_approval_required":
            self.log_panel.append(
                "Seedream 分镜参考图已保存，请检查后确认进入 H3。"
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
            "approval_required",
            "reference_approval_required",
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
                self.approve_button,
                self.approve_seedream_button,
                self.seedream_retry_button,
                self.analysis_retry_button,
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
            self.approve_button,
            self.approve_seedream_button,
            self.seedream_retry_button,
            self.analysis_retry_button,
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
        latest_node = context.node_executions[-1] if context.node_executions else None
        latest_doubao = next(
            (
                item
                for item in reversed(context.node_executions)
                if item.node == "doubao"
            ),
            None,
        )
        package_ready = self._doubao_package_ready(context)
        if (
            context.stage in {PipelineStage.ANALYZING, PipelineStage.WAITING_FOR_APPROVAL}
            and context.approval_status in {ApprovalStatus.PENDING, ApprovalStatus.NOT_REQUIRED}
            and package_ready
            and not context.seedream_references
        ):
            self.approve_button.configure(state="normal")
        if (
            context.stage
            in {
                PipelineStage.FAILED,
                PipelineStage.ANALYZING,
                PipelineStage.WAITING_FOR_APPROVAL,
            }
            and latest_doubao is not None
            and latest_doubao.status in {
                NodeExecutionStatus.FAILED,
                NodeExecutionStatus.RUNNING,
                NodeExecutionStatus.COMPLETED,
            }
            and not package_ready
            and not context.seedream_references
            and (latest_node is latest_doubao or not context.h3_segments)
        ):
            self.analysis_retry_button.configure(state="normal")
        references_ready = self._seedream_references_ready(context)
        if (
            context.stage
            in {
                PipelineStage.GENERATING_REFERENCES,
                PipelineStage.WAITING_FOR_REFERENCE_APPROVAL,
                PipelineStage.FAILED,
            }
            and references_ready
            and not context.h3_segments
        ):
            self.approve_seedream_button.configure(state="normal")
        if (
            context.seedream_references
            and not references_ready
            and context.stage
            in {
                PipelineStage.GENERATING_REFERENCES,
                PipelineStage.WAITING_FOR_REFERENCE_APPROVAL,
                PipelineStage.FAILED,
            }
            and not context.h3_segments
        ):
            self.seedream_retry_button.configure(state="normal")
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

    @staticmethod
    def _seedream_references_ready(context: Any) -> bool:
        if not context.seedream_references:
            return False
        for reference in context.seedream_references:
            if reference.status != "completed" or not reference.output_artifact:
                return False
            path = Path(reference.output_artifact)
            if not path.is_absolute():
                path = context.job_dir / path
            try:
                inspect_image(path)
            except Exception:  # noqa: BLE001 - stale UI artifact becomes retryable
                return False
        return True

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
            payload = context.model_dump(mode="json")
            package_path = self._artifact_path(context, "localization_package")
            if package_path and package_path.is_file():
                payload["doubao_localization_package"] = json.loads(
                    package_path.read_text(encoding="utf-8")
                )
            prompt_path = self._artifact_path(context, "doubao_h3_prompt")
            if prompt_path and prompt_path.is_file():
                payload["doubao_h3_prompt"] = prompt_path.read_text(encoding="utf-8")
            self._set_review(json.dumps(payload, ensure_ascii=False, indent=2))
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

    @staticmethod
    def _doubao_package_ready(context: Any) -> bool:
        package_value = context.artifacts.get("localization_package")
        package_path = (
            Path(package_value)
            if package_value
            else context.job_dir / "json" / "localization_package.json"
        )
        if not package_path.is_absolute():
            package_path = context.job_dir / package_path
        prompt_value = context.artifacts.get("doubao_h3_prompt")
        prompt_path = (
            Path(prompt_value)
            if prompt_value
            else context.job_dir / "json" / "doubao_h3_prompt.txt"
        )
        if not prompt_path.is_absolute():
            prompt_path = context.job_dir / prompt_path
        if not package_path.is_file() or not prompt_path.is_file():
            return False
        try:
            package = json.loads(package_path.read_text(encoding="utf-8"))
            prompt = prompt_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError, TypeError, ValueError):
            return False
        return (
            isinstance(package, dict)
            and isinstance(package.get("h3_prompt"), str)
            and bool(package["h3_prompt"].strip())
            and package["h3_prompt"].strip() == prompt
        )

    def _cancel(self) -> None:
        self.cancel_event.set()
        self.cancel_button.configure(state="disabled")
        self.log_panel.append(
            "已请求取消；已落盘的 Doubao 方案或 Seedream 参考图可在确认/重试后继续，"
            "已创建的 H3 task 可稍后继续等待。"
        )

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
