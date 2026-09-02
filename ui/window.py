"""Tkinter desktop window for video localization jobs."""

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
from core.models import (
    ApprovalStatus,
    ExecutionMode,
    JobSpec,
    NodeExecutionStatus,
    PipelineStage,
)
from core.pipeline import VideoLocalizationPipeline
from language_config import (
    DEFAULT_TARGET_LOCALE_LABEL,
    TARGET_LOCALES,
    locale_from_label,
)
from ui.history import HistoryPanel
from ui.log_panel import LogPanel
from ui.settings import SettingsPanel
from utils.history import HistoryStore


class VideoLocalizerWindow(tk.Tk):
    def __init__(self, config: AppConfig):
        super().__init__()
        self.title("视频入乡随俗工具")
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
        self.character_refs: list[Path] = []
        self.scene_refs: list[Path] = []
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
        run_frame.rowconfigure(7, weight=1)
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
        self.approve_button = ttk.Button(
            buttons,
            text="确认并执行 Seedance",
            command=self._approve,
            state="disabled",
        )
        self.approve_button.pack(side="left", padx=(8, 0))
        self.continue_button = ttk.Button(
            buttons,
            text="继续等待 Seedance",
            command=self._continue_seedance,
            state="disabled",
        )
        self.continue_button.pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="打开输出目录", command=self._open_output).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="复制错误信息", command=self._copy_error).pack(side="left", padx=(8, 0))
        self.retry_button = ttk.Button(buttons, text="重试失败节点", command=self._retry, state="disabled")
        self.retry_button.pack(side="left", padx=(8, 0))

        self.review_frame = ttk.LabelFrame(run_frame, text="Doubao 结果（只读，确认前请检查）", padding=6)
        self.review_frame.grid(row=6, column=0, columnspan=3, sticky="nsew", pady=(10, 0))
        self.review_frame.columnconfigure(0, weight=1)
        self.review_frame.rowconfigure(0, weight=1)
        self.review_text = tk.Text(self.review_frame, height=12, state="disabled", wrap="word")
        review_scrollbar = ttk.Scrollbar(self.review_frame, orient="vertical", command=self.review_text.yview)
        self.review_text.configure(yscrollcommand=review_scrollbar.set)
        self.review_text.grid(row=0, column=0, sticky="nsew")
        review_scrollbar.grid(row=0, column=1, sticky="ns")

        self.log_panel = LogPanel(run_frame)
        self.log_panel.grid(row=7, column=0, columnspan=3, sticky="nsew", pady=(10, 0))

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
        self._set_review("")
        mode = ExecutionMode.AUTO if self.settings.get_auto_continue_to_seedance() else ExecutionMode.MANUAL
        self._run_in_background(config, spec, operation="new", execution_mode=mode)

    def _run_in_background(
        self,
        config: AppConfig,
        spec: JobSpec | None = None,
        *,
        operation: str,
        job_id: str | None = None,
        execution_mode: ExecutionMode = ExecutionMode.MANUAL,
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
                    pipeline.run(spec, execution_mode=execution_mode)
                elif not operation_job_id:
                    raise ValueError("历史任务缺少 job_id")
                elif operation == "approve":
                    pipeline.approve_seedance(operation_job_id)
                elif operation == "continue":
                    pipeline.continue_seedance(operation_job_id)
                elif operation == "retry":
                    pipeline.resume_failed(operation_job_id)
                else:
                    raise ValueError(f"未知操作：{operation}")
            except Exception as exc:  # noqa: BLE001 - pipeline already stores detailed error
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
            PipelineStage.ANALYZING.value: "分析视频",
            PipelineStage.WAITING_FOR_APPROVAL.value: "待确认 Doubao 结果",
            PipelineStage.GENERATING_VIDEO.value: "生成本地化视频（含声音与口型）",
            PipelineStage.COMPLETED.value: "已完成",
            PipelineStage.FAILED.value: "失败",
        }
        progress = int(event.get("progress", 0) or 0)
        if stage:
            self.stage_var.set(stage_labels.get(stage, stage))
        self.progress_var.set(f"{progress}%")
        self.progress_value.set(progress)
        metadata = event.get("metadata", {}) or {}
        if event_type == "log":
            self.log_panel.append(str(event.get("message", "")))
            attempt = metadata.get("attempt")
            if attempt is not None:
                self.retry_var.set(str(attempt))
        elif event_type == "task":
            if metadata.get("task_id"):
                self.task_var.set(str(metadata["task_id"]))
            if metadata.get("request_id"):
                self.request_var.set(str(metadata["request_id"]))
        elif event_type == "approval_required":
            package_path = metadata.get("package_path")
            if package_path:
                self._show_review(Path(str(package_path)))
            self.log_panel.append("Doubao 已完成，请检查只读结果后确认是否进入 Seedance。")
        elif event_type == "error":
            self.last_error = str(event.get("message", ""))
            self.log_panel.append(f"ERROR: {self.last_error}")
        elif event_type == "completed":
            output = metadata.get("output") or event.get("message")
            if output:
                self.last_output = Path(str(output))
                self.log_panel.append(f"输出：{output}")
        if event_type in {"completed", "error", "approval_required", "node_completed", "node_failed"}:
            self.history_panel.refresh()
            self._refresh_current_actions()
    def _set_action_buttons_busy(self, busy: bool) -> None:
        if busy:
            self.start_button.configure(state="disabled")
            self.cancel_button.configure(state="normal")
            for button in (self.approve_button, self.continue_button, self.retry_button):
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
        if self._busy or not self.current_job_id:
            for button in (self.approve_button, self.continue_button, self.retry_button):
                button.configure(state="disabled")
            return
        try:
            context = self.history_store.load_context(self.current_job_id)
        except Exception:
            for button in (self.approve_button, self.continue_button, self.retry_button):
                button.configure(state="disabled")
            return
        for button in (self.approve_button, self.continue_button, self.retry_button):
            button.configure(state="disabled")
        if context.stage == PipelineStage.WAITING_FOR_APPROVAL and context.approval_status == ApprovalStatus.PENDING:
            self.approve_button.configure(state="normal")
        active = next(
            (
                item
                for item in reversed(context.node_executions)
                if item.node == "seedance"
                and item.status == NodeExecutionStatus.RUNNING
                and item.task_id
            ),
            None,
        )
        if active and context.stage in {PipelineStage.GENERATING_VIDEO, PipelineStage.FAILED}:
            self.continue_button.configure(state="normal")
        latest_doubao = next(
            (
                item
                for item in reversed(context.node_executions)
                if item.node == "doubao"
            ),
            None,
        )
        latest_seedance = next(
            (
                item
                for item in reversed(context.node_executions)
                if item.node == "seedance"
            ),
            None,
        )
        package_path = self._artifact_path(context, "localization_package")
        if package_path is None:
            package_path = context.job_dir / "json/localization_package.json"
        analysis_ready = (
            context.stage == PipelineStage.ANALYZING
            and package_path.is_file()
            and latest_doubao is not None
            and latest_doubao.status in {
                NodeExecutionStatus.RUNNING,
                NodeExecutionStatus.COMPLETED,
            }
        )
        if analysis_ready:
            self.approve_button.configure(state="normal")
        final_video = self._artifact_path(context, "final_video")
        seedance_needs_retry = context.stage == PipelineStage.GENERATING_VIDEO and (
            latest_seedance is None
            or (
                latest_seedance.status == NodeExecutionStatus.RUNNING
                and not latest_seedance.task_id
            )
            or (
                latest_seedance.status == NodeExecutionStatus.COMPLETED
                and (final_video is None or not final_video.is_file())
            )
        )
        analysis_needs_retry = context.stage == PipelineStage.ANALYZING and not analysis_ready
        if analysis_needs_retry or seedance_needs_retry:
            self.retry_button.configure(state="normal")
        if context.stage == PipelineStage.FAILED:
            failed_stage = (context.last_error or {}).get("stage")
            if failed_stage == PipelineStage.GENERATING_VIDEO.value and active is None:
                self.retry_button.configure(state="normal")
            elif failed_stage == PipelineStage.ANALYZING.value:
                self.retry_button.configure(state="normal")

    def _approve(self) -> None:
        if self.current_job_id:
            self._run_history_operation("approve")

    def _continue_seedance(self) -> None:
        if self.current_job_id:
            self._run_history_operation("continue")

    def _retry(self) -> None:
        if self.current_job_id:
            self._run_history_operation("retry")

    def _run_history_operation(self, operation: str) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            config = self._effective_config()
            self.settings.save()
            context = self.history_store.load_context(self.current_job_id or "")
            self.current_spec = context.spec
        except Exception as exc:  # noqa: BLE001 - GUI validation message
            messagebox.showerror("任务恢复失败", str(exc))
            return
        self._run_in_background(
            config,
            operation=operation,
            job_id=self.current_job_id,
        )

    def _history_action(self, job_id: str, action: str) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("任务运行中", "请等待当前操作完成后再处理其他历史任务。")
            return
        try:
            context = self.history_store.load_context(job_id)
            self.current_job_id = job_id
            self.current_spec = context.spec
            package_path = self._artifact_path(context, "localization_package")
            if package_path is None:
                package_path = context.job_dir / "json/localization_package.json"
            self.last_output = self._artifact_path(context, "final_video")
            if package_path and package_path.is_file():
                self._show_review(package_path)
            self.notebook.select(0)
        except Exception as exc:  # noqa: BLE001 - selected history may be legacy/corrupt
            messagebox.showerror("无法打开历史任务", str(exc))
            return
        self._run_history_operation(action)

    def _show_review(self, package_path: Path) -> None:
        try:
            payload = json.loads(package_path.read_text(encoding="utf-8"))
            text = json.dumps(payload, ensure_ascii=False, indent=2)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            text = f"Doubao 结果读取失败：{exc}"
        self._set_review(text)

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
        self.log_panel.append("已请求取消，当前外部调用结束后停止后续阶段。")

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
