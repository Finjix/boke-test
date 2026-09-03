"""Local execution-history storage for resumable pipeline jobs."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from threading import RLock
from typing import Any

from core.models import HistoryEntry, JobContext, NodeExecutionStatus, PipelineStage
from utils.artifacts import read_json, write_json
from utils.errors import ValidationError


HISTORY_INDEX_VERSION = 1
HISTORY_PIPELINE_VERSION = 7
_JOB_ID = re.compile(r"^[^\\/:*?\"<>|]+$")


class HistoryStore:
    """Persist and recover history summaries without a database.

    A checkpoint inside each job directory is authoritative. ``history.json``
    is a rebuildable index used for quick summaries; a damaged or stale index
    never prevents a job from being loaded.
    """

    def __init__(self, work_dir: Path):
        self.work_dir = Path(work_dir).expanduser().resolve()
        self.index_path = self.work_dir / "history.json"
        self._lock = RLock()

    def upsert(self, context: JobContext) -> None:
        """Update the index for a valid checkpoint."""

        with self._lock:
            entries = self._scan_entries()
            entries[context.job_id] = self._entry_from_context(context)
            self._write_index(list(entries.values()))

    def list_entries(self) -> list[HistoryEntry]:
        """Return all jobs sorted newest first, repairing the index if needed."""

        with self._lock:
            scanned = self._scan_entries()
            indexed = self._read_index()
            if not indexed or set(indexed) != set(scanned) or any(
                indexed[job_id].model_dump(mode="json")
                != entry.model_dump(mode="json")
                for job_id, entry in scanned.items()
                if job_id not in indexed or indexed[job_id] != entry
            ):
                self._write_index(list(scanned.values()))
            return sorted(
                scanned.values(),
                key=lambda entry: (entry.updated_at or entry.created_at, entry.job_id),
                reverse=True,
            )

    def load_context(self, job_id: str) -> JobContext:
        """Load one checkpoint for read-only inspection."""

        checkpoint = self._checkpoint_path(job_id)
        if not checkpoint.is_file():
            raise ValidationError(f"No checkpoint found for {job_id}")
        try:
            return JobContext.model_validate(read_json(checkpoint))
        except Exception as exc:  # noqa: BLE001 - normalize history errors
            raise ValidationError(f"Invalid checkpoint for {job_id}: {exc}") from exc

    def load_raw(self, job_id: str) -> dict[str, Any]:
        """Load a checkpoint as JSON for read-only history inspection."""

        checkpoint = self._checkpoint_path(job_id)
        if not checkpoint.is_file():
            raise ValidationError(f"No checkpoint found for {job_id}")
        try:
            value = read_json(checkpoint)
        except Exception as exc:  # noqa: BLE001 - normalize history errors
            raise ValidationError(f"Invalid checkpoint for {job_id}: {exc}") from exc
        if not isinstance(value, dict):
            raise ValidationError(f"Invalid checkpoint for {job_id}: root must be an object")
        return value

    def delete(self, job_id: str) -> None:
        """Delete exactly one job directory and rebuild the local index."""

        with self._lock:
            job_dir = self._job_dir(job_id)
            if not job_dir.is_dir():
                raise ValidationError(f"Job directory not found for {job_id}")
            job_dir_resolved = job_dir.resolve()
            work_resolved = self.work_dir.resolve()
            if job_dir_resolved.parent != work_resolved:
                raise ValidationError("Refusing to delete a job outside the work directory")
            shutil.rmtree(job_dir_resolved)
            self._write_index(list(self._scan_entries().values()))

    def _scan_entries(self) -> dict[str, HistoryEntry]:
        entries: dict[str, HistoryEntry] = {}
        if not self.work_dir.is_dir():
            return entries
        try:
            children = list(self.work_dir.iterdir())
        except OSError:
            return entries
        for child in children:
            if not child.is_dir():
                continue
            checkpoint = child / "checkpoint.json"
            if not checkpoint.is_file():
                continue
            try:
                raw = read_json(checkpoint)
            except Exception as exc:  # noqa: BLE001 - retain damaged job visibility
                entries[child.name] = self._incompatible_entry(
                    child,
                    stage="unknown",
                    error=f"无法读取 checkpoint：{exc}",
                )
                continue
            if not isinstance(raw, dict):
                entries[child.name] = self._incompatible_entry(
                    child,
                    stage="unknown",
                    error="checkpoint 根对象不是 JSON object",
                )
                continue
            if raw.get("pipeline_version") != HISTORY_PIPELINE_VERSION:
                entries[child.name] = self._incompatible_entry(
                    child,
                    stage=str(raw.get("stage") or "unknown"),
                    error=(
                        f"pipeline_version={raw.get('pipeline_version')!r} is not supported; "
                        "旧 checkpoint 不可恢复，请新建任务"
                    ),
                    raw=raw,
                )
                continue
            try:
                context = JobContext.model_validate(raw)
            except Exception as exc:  # noqa: BLE001 - old/corrupt jobs stay visible
                entries[child.name] = self._incompatible_entry(
                    child,
                    stage=str(raw.get("stage") or "unknown"),
                    error=f"不可恢复的 checkpoint：{exc}",
                    raw=raw,
                )
                continue
            if context.job_id != child.name:
                entries[child.name] = self._incompatible_entry(
                    child,
                    stage=context.stage.value,
                    error=(
                        "checkpoint job_id does not match its directory; "
                        "该任务不可恢复"
                    ),
                    raw=raw,
                )
                continue
            if context.job_dir.resolve() != child.resolve():
                entries[child.name] = self._incompatible_entry(
                    child,
                    stage=context.stage.value,
                    error=(
                        "checkpoint job_dir does not match its directory; "
                        "该任务不可恢复"
                    ),
                    raw=raw,
                )
                continue
            entries[context.job_id] = self._entry_from_context(context)
        return entries

    def _read_index(self) -> dict[str, HistoryEntry]:
        try:
            payload = read_json(self.index_path)
        except (OSError, UnicodeError, ValueError, TypeError):
            return {}
        if not isinstance(payload, dict) or payload.get("version") != HISTORY_INDEX_VERSION:
            return {}
        jobs = payload.get("jobs")
        if not isinstance(jobs, list):
            return {}
        result: dict[str, HistoryEntry] = {}
        for item in jobs:
            if not isinstance(item, dict) or not isinstance(item.get("job_id"), str):
                continue
            try:
                entry = HistoryEntry.model_validate(item)
            except Exception:
                continue
            result[entry.job_id] = entry
        return result

    def _write_index(self, entries: list[HistoryEntry]) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        write_json(
            self.index_path,
            {
                "version": HISTORY_INDEX_VERSION,
                "jobs": [entry.model_dump(mode="json") for entry in entries],
            },
        )

    def _entry_from_context(self, context: JobContext) -> HistoryEntry:
        latest = context.node_executions[-1] if context.node_executions else None
        output_path = self._artifact_path(context, "final_video")
        return HistoryEntry(
            job_id=context.job_id,
            job_dir=context.job_dir,
            source_name=Path(context.spec.input_video).name,
            target_locale=context.spec.target_locale,
            provider=context.provider,
            stage=context.stage.value,
            status=self._status_from_context(context),
            created_at=context.created_at,
            updated_at=context.updated_at,
            latest_node=latest.node if latest else None,
            latest_attempt=latest.attempt if latest else None,
            last_error=(context.last_error or {}).get("message"),
            output_path=output_path if output_path and output_path.is_file() else None,
            compatible=True,
        )

    @staticmethod
    def _status_from_context(context: JobContext) -> str:
        latest_node = context.node_executions[-1] if context.node_executions else None
        if context.provider == "minimax_h3":
            latest_doubao = next(
                (item for item in reversed(context.node_executions) if item.node == "doubao"),
                None,
            )
            package_ready = HistoryStore._active_doubao_package_ready(context)
            if context.stage == PipelineStage.WAITING_FOR_APPROVAL:
                return "waiting_for_approval" if package_ready else "analysis_interrupted"
            if context.stage == PipelineStage.ANALYZING:
                if latest_doubao and latest_doubao.status == NodeExecutionStatus.RUNNING:
                    return "doubao_running"
                if (
                    latest_doubao
                    and latest_doubao.status == NodeExecutionStatus.COMPLETED
                    and package_ready
                ):
                    return "doubao_ready"
                return "analysis_interrupted"
            if context.stage == PipelineStage.GENERATING_REFERENCES:
                latest_seedream = next(
                    (item for item in reversed(context.node_executions) if item.node == "seedream"),
                    None,
                )
                if latest_seedream and latest_seedream.status == NodeExecutionStatus.RUNNING:
                    return "seedream_running"
                return "seedream_interrupted"
            if context.stage == PipelineStage.WAITING_FOR_REFERENCE_APPROVAL:
                return "waiting_for_reference_approval"
            if context.stage == PipelineStage.WAITING_FOR_SEGMENTS:
                return "waiting_for_segments"
            if context.stage == PipelineStage.WAITING_FOR_NEXT_SEGMENT:
                return "waiting_for_next_segment"
            if context.stage == PipelineStage.GENERATING_SEGMENT:
                if latest_node and latest_node.status == NodeExecutionStatus.RUNNING:
                    return "h3_running" if latest_node.task_id else "h3_interrupted"
                return "h3_interrupted"
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
                    and (latest_node is latest_doubao or not context.h3_segments)
                ):
                    return "doubao_failed"
                if (
                    latest_node
                    and latest_node.status == NodeExecutionStatus.RUNNING
                    and latest_node.task_id
                ):
                    return "h3_running"
                if latest_node and latest_node.status == NodeExecutionStatus.FAILED:
                    return "h3_failed"
                return "failed"
            if context.stage == PipelineStage.COMPLETED:
                return "completed"
            if context.stage == PipelineStage.PREPARING:
                return "preparing"
        package_value = context.artifacts.get("localization_package")
        if package_value:
            package_path = Path(package_value)
            if not package_path.is_absolute():
                package_path = context.job_dir / package_path
        else:
            package_path = context.job_dir / "json/localization_package.json"
        package_ready = package_path.is_file()
        if context.stage == PipelineStage.WAITING_FOR_APPROVAL:
            return "waiting_for_approval"
        if context.stage == PipelineStage.FAILED:
            latest_seedance = next(
                (
                    item
                    for item in reversed(context.node_executions)
                    if item.node == "seedance"
                ),
                None,
            )
            if (
                latest_seedance
                and latest_seedance.status == NodeExecutionStatus.RUNNING
                and latest_seedance.task_id
            ):
                return "seedance_running"
            if latest_seedance and latest_seedance.status == NodeExecutionStatus.FAILED:
                return "seedance_failed"
            return "failed"
        if context.stage == PipelineStage.COMPLETED:
            return "completed"
        if context.stage == PipelineStage.GENERATING_VIDEO:
            if (
                latest_node is not None
                and latest_node.node == "seedance"
                and latest_node.status == NodeExecutionStatus.RUNNING
                and not latest_node.task_id
            ):
                return "seedance_interrupted"
            if latest_node is None:
                return "seedance_interrupted"
            return "seedance_running"
        if context.stage == PipelineStage.ANALYZING and package_ready and latest_node and (
            latest_node.node == "doubao"
            and latest_node.status in {
                NodeExecutionStatus.RUNNING,
                NodeExecutionStatus.COMPLETED,
            }
        ):
            return "analysis_ready"
        if context.stage == PipelineStage.ANALYZING and (
            latest_node is None
            or (
                latest_node.node == "doubao"
                and latest_node.status == NodeExecutionStatus.RUNNING
            )
        ):
            return "analysis_interrupted"
        return context.stage.value

    @staticmethod
    def _active_doubao_package_ready(context: JobContext) -> bool:
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
            package = read_json(package_path)
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

    def _incompatible_entry(
        self,
        job_dir: Path,
        *,
        stage: str,
        error: str,
        raw: dict[str, Any] | None = None,
    ) -> HistoryEntry:
        spec = raw.get("spec") if isinstance(raw, dict) else None
        spec = spec if isinstance(spec, dict) else {}
        source = Path(str(spec.get("input_video") or "")).name
        # The directory name is the only safe identifier for a scanned entry.
        # A malformed checkpoint must not be able to redirect delete/open
        # actions to another path via its embedded job_id.
        job_id = job_dir.name
        return HistoryEntry(
            job_id=job_id,
            job_dir=job_dir.resolve(),
            source_name=source,
            target_locale=str(spec.get("target_locale") or ""),
            stage=stage,
            status="incompatible",
            created_at=str(raw.get("created_at") or "") if isinstance(raw, dict) else "",
            updated_at=str(raw.get("updated_at") or "") if isinstance(raw, dict) else "",
            last_error=error,
            compatible=False,
        )

    def _artifact_path(self, context: JobContext, name: str) -> Path | None:
        value = context.artifacts.get(name)
        if not value:
            return None
        path = Path(value)
        return path if path.is_absolute() else context.job_dir / path

    def _job_dir(self, job_id: str) -> Path:
        if (
            not job_id
            or job_id in {".", ".."}
            or not _JOB_ID.fullmatch(job_id)
            or Path(job_id).name != job_id
        ):
            raise ValidationError("Invalid job ID")
        return self.work_dir / job_id

    def _checkpoint_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "checkpoint.json"
