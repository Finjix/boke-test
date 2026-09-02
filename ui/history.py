"""Execution-history panel for persisted, resumable localization jobs."""

from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

from core.models import (
    ApprovalStatus,
    HistoryEntry,
    JobContext,
    NodeExecutionStatus,
    PipelineStage,
)
from utils.history import HistoryStore


STAGE_LABELS = {
    "analyzing": "分析视频",
    "waiting_for_approval": "待确认 Doubao",
    "generating_video": "Seedance 处理中",
    "completed": "已完成",
    "failed": "失败",
    "unknown": "未知",
}

STATUS_LABELS = {
    "analyzing": "分析中",
    "analysis_ready": "Doubao 已完成，待继续",
    "analysis_interrupted": "分析需重试",
    "waiting_for_approval": "待人工确认",
    "seedance_running": "Seedance 处理中",
    "seedance_interrupted": "Seedance 需恢复",
    "seedance_failed": "Seedance 可重试",
    "completed": "已完成",
    "failed": "失败",
    "incompatible": "不可恢复",
    "unknown": "未知",
}


class HistoryPanel(ttk.Frame):
    """List persisted jobs and expose safe recovery actions."""

    def __init__(
        self,
        master: tk.Misc,
        history_store: HistoryStore,
        *,
        action_callback: Callable[[str, str], None],
        open_path_callback: Callable[[Path], None],
    ):
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
        self.approve_button = ttk.Button(
            toolbar,
            text="确认并执行 Seedance",
            command=lambda: self._dispatch("approve"),
            state="disabled",
        )
        self.approve_button.pack(side="left", padx=(8, 0))
        self.continue_button = ttk.Button(
            toolbar,
            text="继续等待 Seedance",
            command=lambda: self._dispatch("continue"),
            state="disabled",
        )
        self.continue_button.pack(side="left", padx=(8, 0))
        self.retry_button = ttk.Button(
            toolbar,
            text="重试失败节点",
            command=lambda: self._dispatch("retry"),
            state="disabled",
        )
        self.retry_button.pack(side="left", padx=(8, 0))
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
            "stage": "节点",
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

        detail_frame = ttk.LabelFrame(pane, text="任务详情", padding=6)
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
        except Exception as exc:  # noqa: BLE001 - history must not prevent app startup
            self._set_detail(f"读取执行历史失败：{exc}")
            return
        self.entries = {entry.job_id: entry for entry in entries}
        self.tree.delete(*self.tree.get_children())
        for entry in entries:
            stage_key = entry.stage
            if entry.stage == PipelineStage.FAILED.value and entry.latest_node:
                stage_key = (
                    PipelineStage.ANALYZING.value
                    if entry.latest_node == "doubao"
                    else PipelineStage.GENERATING_VIDEO.value
                )
            self.tree.insert(
                "",
                "end",
                iid=entry.job_id,
                values=(
                    entry.updated_at or entry.created_at or "-",
                    entry.source_name or "-",
                    entry.target_locale or "-",
                    STAGE_LABELS.get(stage_key, stage_key),
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
            for button in (
                self.approve_button,
                self.continue_button,
                self.retry_button,
                self.open_dir_button,
                self.open_output_button,
                self.delete_button,
            ):
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
            except Exception as exc:  # noqa: BLE001 - surface the selected job issue
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
            f"执行模式：{context.execution_mode.value}",
            f"创建时间：{context.created_at}",
            f"更新时间：{context.updated_at}",
            "",
            "节点执行时间线：",
        ]
        if not context.node_executions:
            lines.append("  暂无节点执行记录")
        for node in context.node_executions:
            lines.extend(
                [
                    f"  - {node.node} attempt {node.attempt}: {node.status.value}",
                    f"    开始：{node.started_at}",
                    f"    结束：{node.finished_at or '未结束'}",
                    f"    task：{node.task_id or '-'}",
                    f"    请求：{', '.join(node.request_ids) or '-'}",
                    f"    输出：{', '.join(node.output_artifacts) or '-'}",
                ]
            )
            for call in node.provider_calls:
                lines.append(
                    "    provider call: "
                    f"{call.status.value}, request={call.request_id or '-'}, "
                    f"raw={call.raw_response_path or '-'}"
                )
            if node.error:
                lines.append(f"    错误：{node.error.get('message', node.error)}")
        if context.last_error:
            lines.extend(
                [
                    "",
                    "最近错误：",
                    f"  {context.last_error.get('message', context.last_error)}",
                    f"  provider：{context.last_error.get('provider') or '-'}",
                    f"  request：{context.last_error.get('request_id') or '-'}",
                    f"  raw：{context.last_error.get('raw_response_path') or '-'}",
                ]
            )
        package_path = self._artifact_path(context, "localization_package")
        if package_path is None:
            package_path = context.job_dir / "json/localization_package.json"
        if package_path and package_path.is_file():
            try:
                package = json.loads(package_path.read_text(encoding="utf-8"))
                speakers = package.get("speakers", []) if isinstance(package, dict) else []
                dialogues = package.get("dialogues", []) if isinstance(package, dict) else []
                visual = package.get("visual_localization", {}) if isinstance(package, dict) else {}
                cultural = package.get("cultural_requirements", []) if isinstance(package, dict) else []
                lines.extend(
                    [
                        "",
                        "Doubao 摘要（只读）：",
                        f"  人物：{len(speakers) if isinstance(speakers, list) else 0} 个",
                        f"  对白：{len(dialogues) if isinstance(dialogues, list) else 0} 条",
                        f"  本地化规划：{', '.join(str(key) for key in visual) if isinstance(visual, dict) else '-'}",
                        f"  文化要求：{len(cultural) if isinstance(cultural, list) else 0} 条",
                    ]
                )
                if isinstance(dialogues, list):
                    for dialogue in dialogues[:8]:
                        if not isinstance(dialogue, dict):
                            continue
                        lines.append(
                            "  对白："
                            f"{dialogue.get('speaker_id', '-')} "
                            f"{dialogue.get('source_text', '')} → {dialogue.get('target_text', '')}"
                        )
                lines.extend(
                    [
                        "",
                        "Doubao Localization Package（完整 JSON，只读）：",
                        json.dumps(package, ensure_ascii=False, indent=2),
                    ]
                )
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                lines.extend(["", f"Doubao 结果读取失败：{exc}"])
        self._set_detail("\n".join(lines))

    @staticmethod
    def _status(context: JobContext) -> str:
        if context.stage == PipelineStage.WAITING_FOR_APPROVAL:
            return "waiting_for_approval"
        if context.stage == PipelineStage.COMPLETED:
            return "completed"
        if context.stage == PipelineStage.ANALYZING:
            package_path = HistoryPanel._artifact_path(context, "localization_package")
            if package_path is None:
                package_path = context.job_dir / "json/localization_package.json"
            latest = next(
                (item for item in reversed(context.node_executions) if item.node == "doubao"),
                None,
            )
            if package_path.is_file() and latest and latest.status in {
                NodeExecutionStatus.RUNNING,
                NodeExecutionStatus.COMPLETED,
            }:
                return "analysis_ready"
            if latest is None or latest.status == NodeExecutionStatus.RUNNING:
                return "analysis_interrupted"
            return "analyzing"
        if context.stage == PipelineStage.GENERATING_VIDEO:
            if not context.node_executions or not any(
                item.node == "seedance" for item in context.node_executions
            ):
                return "seedance_interrupted"
            return "seedance_running"
        if context.stage == PipelineStage.FAILED:
            node = next(
                (item for item in reversed(context.node_executions) if item.node == "seedance"),
                None,
            )
            if node and node.status == NodeExecutionStatus.RUNNING and node.task_id:
                return "seedance_running"
            return "seedance_failed" if node and node.status == NodeExecutionStatus.FAILED else "failed"
        return context.stage.value

    def _update_actions(
        self,
        entry: HistoryEntry | None,
        context: JobContext | None,
    ) -> None:
        for button in (
            self.approve_button,
            self.continue_button,
            self.retry_button,
            self.open_dir_button,
            self.open_output_button,
            self.delete_button,
        ):
            button.configure(state="disabled")
        if self._busy:
            return
        if not entry or not entry.compatible or not context:
            return
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
        latest_analysis_ready = (
            context.stage == PipelineStage.ANALYZING
            and package_path.is_file()
            and latest_doubao is not None
            and latest_doubao.status in {
                NodeExecutionStatus.RUNNING,
                NodeExecutionStatus.COMPLETED,
            }
        )
        if (
            context.stage == PipelineStage.WAITING_FOR_APPROVAL
            and context.approval_status == ApprovalStatus.PENDING
        ) or latest_analysis_ready:
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
        analysis_needs_retry = context.stage == PipelineStage.ANALYZING and not latest_analysis_ready
        if analysis_needs_retry or seedance_needs_retry:
            self.retry_button.configure(state="normal")
        if (
            context.stage == PipelineStage.FAILED
            and (context.last_error or {}).get("stage") == PipelineStage.GENERATING_VIDEO.value
            and active is None
        ):
            self.retry_button.configure(state="normal")
        self.open_dir_button.configure(state="normal")
        if entry.output_path and entry.output_path.is_file():
            self.open_output_button.configure(state="normal")
        self.delete_button.configure(state="normal")

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
        output_dir = (
            entry.output_path.parent
            if entry.output_path and entry.output_path.is_file()
            else entry.job_dir / "output"
        )
        self.open_path_callback(output_dir)

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
            if context:
                if any(
                    item.node == "doubao"
                    and item.status == NodeExecutionStatus.RUNNING
                    for item in context.node_executions
                ):
                    messagebox.showwarning("无法删除", "Doubao 节点仍在运行，请先停止当前任务。")
                    return
                if any(
                    item.node == "seedance"
                    and item.status == NodeExecutionStatus.RUNNING
                    and item.task_id
                    for item in context.node_executions
                ):
                    messagebox.showwarning("无法删除", "任务仍有云端 Seedance task，请先继续或重试完成。")
                    return
        if not messagebox.askyesno(
            "确认删除",
            f"确定删除任务 {entry.job_id} 及其全部节点结果吗？此操作不可恢复。",
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
    def _artifact_path(context: JobContext, name: str) -> Path | None:
        value = context.artifacts.get(name)
        if not value:
            return None
        path = Path(value)
        return path if path.is_absolute() else context.job_dir / path
