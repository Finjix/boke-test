"""Read-only execution history and safe H3 recovery actions."""

from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

from core.models import HistoryEntry, JobContext, NodeExecutionStatus, PipelineStage
from media.images import inspect_image
from utils.history import HistoryStore


STAGE_LABELS = {
    "preparing": "准备任务",
    "analyzing": "Doubao 分析",
    "waiting_for_approval": "等待确认 Doubao 方案",
    "generating_references": "Seedream 生成分镜图",
    "waiting_for_reference_approval": "等待确认分镜图",
    "waiting_for_segments": "等待上传片段",
    "generating_segment": "H3 生成片段",
    "waiting_for_next_segment": "等待下一片",
    "completed": "已完成",
    "failed": "失败",
    "unknown": "未知",
}

STATUS_LABELS = {
    "preparing": "准备中",
    "waiting_for_segments": "等待片段",
    "waiting_for_next_segment": "可上传下一片",
    "waiting_for_approval": "待确认 Doubao 方案",
    "doubao_running": "Doubao 处理中",
    "doubao_ready": "Doubao 已完成",
    "analysis_interrupted": "Doubao 需重试",
    "doubao_failed": "Doubao 可重试",
    "seedream_running": "Seedream 处理中",
    "seedream_interrupted": "Seedream 需重试",
    "seedream_failed": "Seedream 可重试",
    "waiting_for_reference_approval": "待确认 Seedream 参考图",
    "h3_running": "H3 处理中",
    "h3_interrupted": "H3 需继续",
    "h3_failed": "H3 可重试",
    "completed": "已完成",
    "failed": "失败",
    "incompatible": "不可恢复",
    "unknown": "未知",
}


class HistoryPanel(ttk.Frame):
    """List persisted jobs and expose explicit analysis/H3 recovery actions."""

    def __init__(
        self,
        master: tk.Misc,
        history_store: HistoryStore,
        *,
        action_callback: Callable[[str, str], None],
        open_path_callback: Callable[[Path], None],
    ) -> None:
        super().__init__(master, padding=8)
        self.history_store = history_store
        self.action_callback = action_callback
        self.open_path_callback = open_path_callback
        self.entries: dict[str, HistoryEntry] = {}
        self.selected_job_id: str | None = None
        self._busy = False
        self._build()
        self.refresh()

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(self)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(toolbar, text="刷新历史", command=self.refresh).pack(side="left")
        self.append_button = ttk.Button(
            toolbar,
            text="上传下一片",
            command=lambda: self._dispatch("append"),
            state="disabled",
        )
        self.append_button.pack(side="left", padx=(8, 0))
        self.approve_button = ttk.Button(
            toolbar,
            text="确认 Doubao，生成参考图",
            command=lambda: self._dispatch("approve_doubao"),
            state="disabled",
        )
        self.approve_button.pack(side="left", padx=(8, 0))
        self.approve_seedream_button = ttk.Button(
            toolbar,
            text="确认参考图并进入 H3",
            command=lambda: self._dispatch("approve_seedream"),
            state="disabled",
        )
        self.approve_seedream_button.pack(side="left", padx=(8, 0))
        self.retry_seedream_button = ttk.Button(
            toolbar,
            text="重试 Seedream",
            command=lambda: self._dispatch("retry_seedream"),
            state="disabled",
        )
        self.retry_seedream_button.pack(side="left", padx=(8, 0))
        self.retry_analysis_button = ttk.Button(
            toolbar,
            text="重试 Doubao",
            command=lambda: self._dispatch("retry_doubao"),
            state="disabled",
        )
        self.retry_analysis_button.pack(side="left", padx=(8, 0))
        self.continue_button = ttk.Button(
            toolbar,
            text="继续等待 H3",
            command=lambda: self._dispatch("continue"),
            state="disabled",
        )
        self.continue_button.pack(side="left", padx=(8, 0))
        self.retry_button = ttk.Button(
            toolbar,
            text="重试当前片段",
            command=lambda: self._dispatch("retry"),
            state="disabled",
        )
        self.retry_button.pack(side="left", padx=(8, 0))
        self.finish_button = ttk.Button(
            toolbar,
            text="完成拼接",
            command=lambda: self._dispatch("finish"),
            state="disabled",
        )
        self.finish_button.pack(side="left", padx=(8, 0))
        self.open_dir_button = ttk.Button(
            toolbar,
            text="打开任务目录",
            command=self._open_selected_dir,
            state="disabled",
        )
        self.open_dir_button.pack(side="left", padx=(8, 0))
        self.open_output_button = ttk.Button(
            toolbar,
            text="打开输出",
            command=self._open_selected_output,
            state="disabled",
        )
        self.open_output_button.pack(side="left", padx=(8, 0))
        self.delete_button = ttk.Button(
            toolbar,
            text="删除历史",
            command=self._delete_selected,
            state="disabled",
        )
        self.delete_button.pack(side="right")

        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.grid(row=1, column=0, sticky="nsew")

        list_frame = ttk.Frame(pane, padding=(0, 0, 8, 0))
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            list_frame,
            columns=("updated", "source", "locale", "stage", "status", "error", "job_id"),
            show="headings",
            selectmode="browse",
        )
        headings = {
            "updated": "更新时间",
            "source": "源视频",
            "locale": "地区",
            "stage": "当前节点",
            "status": "状态",
            "error": "错误摘要",
            "job_id": "任务 ID",
        }
        widths = {
            "updated": 150,
            "source": 170,
            "locale": 85,
            "stage": 125,
            "status": 105,
            "error": 220,
            "job_id": 235,
        }
        for column, heading in headings.items():
            self.tree.heading(column, text=heading)
            self.tree.column(column, width=widths[column], anchor="w")
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        pane.add(list_frame, weight=1)

        detail_frame = ttk.LabelFrame(pane, text="任务详情（只读）", padding=6)
        detail_frame.columnconfigure(0, weight=1)
        detail_frame.rowconfigure(0, weight=1)
        self.detail = tk.Text(detail_frame, width=72, height=20, state="disabled", wrap="word")
        detail_scrollbar = ttk.Scrollbar(detail_frame, orient="vertical", command=self.detail.yview)
        self.detail.configure(yscrollcommand=detail_scrollbar.set)
        self.detail.grid(row=0, column=0, sticky="nsew")
        detail_scrollbar.grid(row=0, column=1, sticky="ns")
        pane.add(detail_frame, weight=2)

    def refresh(self) -> None:
        previous = self.selected_job_id
        try:
            entries = self.history_store.list_entries()
        except Exception as exc:  # noqa: BLE001 - history must not block startup
            self._set_detail(f"读取执行历史失败：{exc}")
            return
        self.entries = {entry.job_id: entry for entry in entries}
        self.tree.delete(*self.tree.get_children())
        for entry in entries:
            self.tree.insert(
                "",
                "end",
                iid=entry.job_id,
                values=(
                    entry.updated_at or entry.created_at or "-",
                    entry.source_name or "-",
                    entry.target_locale or "-",
                    STAGE_LABELS.get(entry.stage, entry.stage),
                    STATUS_LABELS.get(entry.status, entry.status),
                    (entry.last_error or "-")[:100],
                    entry.job_id,
                ),
            )
        if previous and previous in self.entries:
            self.tree.selection_set(previous)
            self.tree.focus(previous)
        elif entries:
            self.tree.selection_set(entries[0].job_id)
            self.tree.focus(entries[0].job_id)
        else:
            self.selected_job_id = None
            self._set_detail("暂无执行历史。")
            self._update_actions(None, None)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        if busy:
            for button in self._action_buttons():
                button.configure(state="disabled")
            return
        self._on_select(None)

    def selected_entry(self) -> HistoryEntry | None:
        return self.entries.get(self.selected_job_id or "")

    def _on_select(self, _event: object | None) -> None:
        selection = self.tree.selection()
        self.selected_job_id = selection[0] if selection else None
        entry = self.entries.get(self.selected_job_id or "")
        context: JobContext | None = None
        if entry and entry.compatible:
            try:
                context = self.history_store.load_context(entry.job_id)
                self._render_context(context)
            except Exception as exc:  # noqa: BLE001 - surface selected issue
                self._set_detail(f"读取任务详情失败：{exc}")
        elif entry:
            self._set_detail(
                f"任务：{entry.job_id}\n状态：不可恢复\n\n{entry.last_error or 'checkpoint 无法解析。'}"
            )
        else:
            self._set_detail("请选择一个历史任务。")
        self._update_actions(entry, context)

    def _render_context(self, context: JobContext) -> None:
        lines = [
            f"任务 ID：{context.job_id}",
            f"源视频：{Path(context.spec.input_video).name}",
            f"目标地区：{context.spec.target_region} / {context.spec.target_locale}",
            f"当前状态：{STATUS_LABELS.get(self._status(context), context.stage.value)}",
            f"主视频时长：{context.source_master_duration_seconds or '-'} 秒",
            f"创建时间：{context.created_at}",
            f"更新时间：{context.updated_at}",
            "",
            "Doubao / Seedream / H3 节点时间线：",
        ]
        for node in context.node_executions:
            lines.extend(
                [
                    f"  - {node.node} attempt {node.attempt}: {node.status.value}",
                    f"    provider：{node.provider or '-'}",
                    f"    request：{', '.join(node.request_ids) or '-'}",
                    f"    task：{node.task_id or '-'}",
                    f"    输入：{', '.join(node.input_artifacts) or '-'}",
                    f"    输出：{', '.join(node.output_artifacts) or '-'}",
                ]
            )
            for call in node.provider_calls:
                lines.append(
                    f"    provider call：{call.status.value}, request={call.request_id or '-'}, "
                    f"raw={call.raw_response_path or '-'}"
                )
        package_path = context.artifacts.get("localization_package")
        prompt_path = context.artifacts.get("doubao_h3_prompt")
        lines.extend(
            [
                "",
                f"Doubao package：{package_path or '-'}",
                f"Doubao H3 prompt：{prompt_path or '-'}",
                f"Seedream manifest：{context.artifacts.get('seedream_reference_manifest') or '-'}",
                "",
                "Seedream 分镜参考图：",
            ]
        )
        if not context.seedream_references:
            lines.append("  尚未生成分镜参考图")
        for reference in context.seedream_references:
            lines.extend(
                [
                    f"  - {reference.shot_id}: {reference.status}, "
                    f"源时间 {reference.start_ms}–{reference.end_ms} ms, "
                    f"关键帧 {reference.keyframe_ms} ms, 连续组 {reference.continuity_group}",
                    f"    源关键帧：{reference.source_frame_artifact or '-'}",
                    f"    目标参考图：{reference.output_artifact or '-'}",
                    f"    远程参考图：{reference.reference_asset.remote_url if reference.reference_asset else '-'}",
                    f"    提示词：{reference.prompt}",
                ]
            )
            for attempt in reference.attempts:
                lines.extend(
                    [
                        f"    attempt {attempt.attempt}: {attempt.status.value}",
                        f"      request：{attempt.request_id or '-'}",
                        f"      请求：{attempt.request_artifact or '-'}",
                        f"      连续性参考镜头：{attempt.continuity_reference_shot_id or '-'}",
                        f"      连续性参考图：{attempt.continuity_reference_artifact or '-'}",
                        f"      全部连续性参考镜头：{', '.join(attempt.continuity_reference_shot_ids) or '-'}",
                        f"      连续性参考图省略数：{attempt.continuity_references_omitted}",
                        f"      原始响应：{attempt.raw_response_artifact or '-'}",
                        f"      规范化响应：{attempt.response_artifact or '-'}",
                        f"      Provider 输出：{attempt.provider_output_artifact or '-'}",
                        f"      结果：{attempt.output_artifact or '-'}",
                        f"      失败记录：{attempt.failure_artifact or '-'}",
                    ]
                )
                if attempt.error:
                    lines.append(f"      错误：{attempt.error.get('message', attempt.error)}")
        lines.extend(
            [
                "",
                "H3 片段时间线：",
            ]
        )
        if not context.h3_segments:
            lines.append("  尚未上传片段")
        for segment in context.h3_segments:
            lines.extend(
                [
                    f"  - segment {segment.index}: {segment.status}, "
                    f"原始 {segment.source_duration_seconds:.3f}s / H3 {segment.normalized_duration_seconds}s",
                    f"    参考策略：{segment.reference_strategy}",
                    f"    当前分镜参考镜头：{', '.join(segment.reference_shot_ids) or '-'}",
                    f"    前片连续性参考镜头：{', '.join(segment.continuity_reference_shot_ids) or '-'}",
                    f"    前片连续性参考图省略数：{segment.continuity_references_omitted}",
                    f"    当前输出：{segment.output_artifact or '-'}",
                    "    提示词：",
                    f"      {segment.prompt}",
                ]
            )
            for attempt in segment.attempts:
                lines.extend(
                    [
                        f"    attempt {attempt.attempt}: {attempt.status.value}",
                        f"      task：{attempt.task_id or '-'}",
                        f"      request：{attempt.request_id or '-'}",
                        f"      开始：{attempt.started_at}",
                        f"      结束：{attempt.finished_at or '未结束'}",
                        f"      content：{attempt.content_artifact or '-'}",
                        f"      创建响应：{attempt.create_response_artifact or '-'}",
                        f"      最终响应：{attempt.final_response_artifact or '-'}",
                        f"      失败记录：{attempt.failure_artifact or '-'}",
                        f"      Provider 原始输出：{attempt.provider_output_artifact or '-'}",
                        f"      输出：{attempt.output_artifact or '-'}",
                    ]
                )
                if attempt.error:
                    lines.append(f"      错误：{attempt.error.get('message', attempt.error)}")
        if context.spec.reference_images:
            # Legacy v4/v5/v6 records remain visible in their own renderer;
            # active v7 jobs should never populate this field.
            lines.extend(["", f"旧版用户参考图：{len(context.spec.reference_images)} 张（只读）"])
        if context.last_error:
            lines.extend(
                [
                    "",
                    "最近错误：",
                    f"  {context.last_error.get('message', context.last_error)}",
                    f"  provider：{context.last_error.get('provider') or '-'}",
                    f"  request：{context.last_error.get('request_id') or '-'}",
                ]
            )
        self._set_detail("\n".join(lines))

    @staticmethod
    def _status(context: JobContext) -> str:
        latest = context.node_executions[-1] if context.node_executions else None
        latest_doubao = next(
            (item for item in reversed(context.node_executions) if item.node == "doubao"),
            None,
        )
        if context.stage == PipelineStage.COMPLETED:
            return "completed"
        if context.stage == PipelineStage.WAITING_FOR_APPROVAL:
            return (
                "waiting_for_approval"
                if HistoryPanel._doubao_package_ready(context)
                else "analysis_interrupted"
            )
        if context.stage == PipelineStage.ANALYZING:
            if latest_doubao and latest_doubao.status == NodeExecutionStatus.RUNNING:
                return "doubao_running"
            return (
                "doubao_ready"
                if latest_doubao
                and latest_doubao.status == NodeExecutionStatus.COMPLETED
                and HistoryPanel._doubao_package_ready(context)
                else "analysis_interrupted"
            )
        if context.stage == PipelineStage.GENERATING_REFERENCES:
            latest_seedream = next(
                (item for item in reversed(context.node_executions) if item.node == "seedream"),
                None,
            )
            return (
                "seedream_running"
                if latest_seedream and latest_seedream.status == NodeExecutionStatus.RUNNING
                else "seedream_interrupted"
            )
        if context.stage == PipelineStage.WAITING_FOR_REFERENCE_APPROVAL:
            return "waiting_for_reference_approval"
        if context.stage == PipelineStage.WAITING_FOR_SEGMENTS:
            return "waiting_for_segments"
        if context.stage == PipelineStage.WAITING_FOR_NEXT_SEGMENT:
            return "waiting_for_next_segment"
        if context.stage == PipelineStage.GENERATING_SEGMENT:
            return "h3_running" if latest and latest.task_id else "h3_interrupted"
        if context.stage == PipelineStage.FAILED:
            latest_seedream = next(
                (item for item in reversed(context.node_executions) if item.node == "seedream"),
                None,
            )
            if latest_seedream and latest_seedream.status == NodeExecutionStatus.FAILED:
                return "seedream_failed"
            if latest_seedream and latest_seedream.status == NodeExecutionStatus.RUNNING:
                return "seedream_interrupted"
            if (
                latest_doubao
                and latest_doubao.status == NodeExecutionStatus.FAILED
                and (latest is latest_doubao or not context.h3_segments)
            ):
                return "doubao_failed"
            if latest and latest.status == NodeExecutionStatus.RUNNING and latest.task_id:
                return "h3_running"
            return "h3_failed" if latest and latest.status == NodeExecutionStatus.FAILED else "failed"
        return context.stage.value

    def _update_actions(self, entry: HistoryEntry | None, context: JobContext | None) -> None:
        for button in self._action_buttons():
            button.configure(state="disabled")
        if self._busy or not entry or not entry.compatible or not context:
            return
        latest = context.node_executions[-1] if context.node_executions else None
        latest_doubao = next(
            (item for item in reversed(context.node_executions) if item.node == "doubao"),
            None,
        )
        package_ready = self._doubao_package_ready(context)
        if (
            context.stage in {PipelineStage.ANALYZING, PipelineStage.WAITING_FOR_APPROVAL}
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
            and latest_doubao.status
            in {
                NodeExecutionStatus.FAILED,
                NodeExecutionStatus.RUNNING,
                NodeExecutionStatus.COMPLETED,
            }
            and not package_ready
            and not context.seedream_references
            and (latest is latest_doubao or not context.h3_segments)
        ):
            self.retry_analysis_button.configure(state="normal")
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
            self.retry_seedream_button.configure(state="normal")
        incomplete = next(
            (segment for segment in context.h3_segments if segment.status != "completed"), None
        )
        if context.stage in {
            PipelineStage.WAITING_FOR_SEGMENTS,
            PipelineStage.WAITING_FOR_NEXT_SEGMENT,
        }:
            self.append_button.configure(state="normal")
        if incomplete:
            active = next(
                (
                    attempt
                    for attempt in incomplete.attempts
                    if attempt.attempt == incomplete.active_attempt
                    and attempt.status == NodeExecutionStatus.RUNNING
                ),
                None,
            )
            if active and active.task_id:
                self.continue_button.configure(state="normal")
            elif incomplete.attempts and incomplete.attempts[-1].status == NodeExecutionStatus.FAILED:
                self.retry_button.configure(state="normal")
        if (
            context.stage == PipelineStage.WAITING_FOR_NEXT_SEGMENT
            and context.h3_segments
            and all(segment.status == "completed" for segment in context.h3_segments)
        ):
            self.finish_button.configure(state="normal")
        self.open_dir_button.configure(state="normal")
        if entry.output_path and entry.output_path.is_file():
            self.open_output_button.configure(state="normal")
        self.delete_button.configure(state="normal")

    @staticmethod
    def _seedream_references_ready(context: JobContext) -> bool:
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

    def _action_buttons(self):
        return (
            self.approve_button,
            self.approve_seedream_button,
            self.retry_seedream_button,
            self.retry_analysis_button,
            self.append_button,
            self.continue_button,
            self.retry_button,
            self.finish_button,
            self.open_dir_button,
            self.open_output_button,
            self.delete_button,
        )

    def _dispatch(self, action: str) -> None:
        if self.selected_job_id:
            self.action_callback(self.selected_job_id, action)

    def _open_selected_dir(self) -> None:
        entry = self.selected_entry()
        if entry:
            self.open_path_callback(entry.job_dir)

    def _open_selected_output(self) -> None:
        entry = self.selected_entry()
        if not entry:
            return
        target = entry.output_path.parent if entry.output_path else entry.job_dir / "output"
        self.open_path_callback(target)

    def _delete_selected(self) -> None:
        if self._busy:
            return
        entry = self.selected_entry()
        if not entry:
            return
        if entry.compatible:
            try:
                context = self.history_store.load_context(entry.job_id)
            except Exception:
                context = None
            if context and (
                any(
                    attempt.status == NodeExecutionStatus.RUNNING and attempt.task_id
                    for segment in context.h3_segments
                    for attempt in segment.attempts
                )
                or any(
                    node.node in {"doubao", "seedream"}
                    and node.status == NodeExecutionStatus.RUNNING
                    for node in context.node_executions
                )
            ):
                messagebox.showwarning("无法删除", "仍有云端节点运行，请先完成或恢复。")
                return
        if not messagebox.askyesno(
            "确认删除",
            f"确定删除任务 {entry.job_id} 及其全部片段证据吗？此操作不可恢复。",
        ):
            return
        try:
            self.history_store.delete(entry.job_id)
        except Exception as exc:  # noqa: BLE001 - display filesystem failure
            messagebox.showerror("删除失败", str(exc))
            return
        self.selected_job_id = None
        self.refresh()

    def _set_detail(self, message: str) -> None:
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", message)
        self.detail.configure(state="disabled")

    @staticmethod
    def _doubao_package_ready(context: JobContext) -> bool:
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
            and isinstance(package.get("reference_shots"), list)
            and bool(package["reference_shots"])
            and package["h3_prompt"].strip() == prompt
        )
