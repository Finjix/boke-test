"""Stateful orchestration for one v4 video localization job."""

from __future__ import annotations

import hashlib
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from api.ark import ArkClient
from api.seedance import SeedanceClient
from api.uguu import UguuClient
from config import AppConfig
from core.localization import ANALYSIS_PROMPT_VERSION, analyze_video
from core.models import (
    ApprovalStatus,
    ExecutionMode,
    JobContext,
    JobSpec,
    LocalizationPackage,
    NodeExecution,
    NodeExecutionStatus,
    PipelineResult,
    PipelineEvent,
    PipelineStage,
    ProviderCall,
    UploadedAsset,
)
from core.preflight import require_preflight, run_preflight
from core.seedance_prompt import SEEDANCE_PROMPT_VERSION, build_seedance_content
from media import ffprobe
from media.downloader import download
from utils.artifacts import read_json, write_json
from utils.errors import (
    ErrorRecord,
    MediaCommandError,
    PipelineCancelled,
    PreflightError,
    ProviderError,
    ValidationError,
    VideoLocalizerError,
)
from utils.ids import new_job_id
from utils.history import HistoryStore
from utils.logger import JobLogger
from video_config import validate_duration


PIPELINE_VERSION = 4
PIPELINE_STAGES: tuple[PipelineStage, ...] = (
    PipelineStage.ANALYZING,
    PipelineStage.WAITING_FOR_APPROVAL,
    PipelineStage.GENERATING_VIDEO,
)

_PROGRESS = {
    PipelineStage.ANALYZING: 25,
    PipelineStage.WAITING_FOR_APPROVAL: 30,
    PipelineStage.GENERATING_VIDEO: 95,
}

_STAGE_METRIC = {
    PipelineStage.ANALYZING: "analysis_duration",
    PipelineStage.GENERATING_VIDEO: "seedance_duration",
}

_V4_ARTIFACTS = {
    "preflight",
    "source_video",
    "references",
    "source_info",
    "assets",
    "localization_package",
    "seedance_content",
    "seedance_result",
    "final_info",
    "final_video",
}

_V4_CACHE_KEY_FIELDS = {
    "source_video_hash",
    "target_language",
    "target_region",
    "target_locale",
    "doubao_model",
    "seedance_model",
    "analysis_prompt_version",
    "seedance_prompt_version",
}

_V4_TASK_KEYS = {"seedance"}

_DOUBAO_NODE = "doubao"
_SEEDANCE_NODE = "seedance"
_SEEDANCE_TERMINAL_ERRORS = {
    "FAILED",
    "CANCELLED",
    "EXPIRED",
    "UNKNOWN_STATUS",
    "VIDEO_URL_MISSING",
    "EMPTY_VIDEO_RESULT",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class VideoLocalizationPipeline:
    def __init__(
        self,
        config: AppConfig,
        *,
        ark_client: Any | None = None,
        uguu_client: Any | None = None,
        seedance_client: Any | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        cancel_event: threading.Event | None = None,
        history_store: HistoryStore | None = None,
    ):
        self.config = config
        self.event_callback = event_callback
        self.cancel_event = cancel_event or threading.Event()
        self._logger: JobLogger | None = None
        self.ark_client = ark_client
        self.uguu_client = uguu_client
        self.seedance_client = seedance_client
        self.history_store = history_store or HistoryStore(config.work_dir)

    def _ensure_clients(self) -> None:
        self.ark_client = self.ark_client or ArkClient(self.config, logger=self._logger)
        self.uguu_client = self.uguu_client or UguuClient(self.config, logger=self._logger)
        self.seedance_client = self.seedance_client or SeedanceClient(
            self.config,
            logger=self._logger,
        )

    def run(
        self,
        spec: JobSpec,
        *,
        job_id: str | None = None,
        resume_from: PipelineStage | str | None = None,
        skip_preflight: bool = False,
        execution_mode: ExecutionMode | str = ExecutionMode.MANUAL,
    ) -> PipelineResult:
        run_started = time.monotonic()
        try:
            requested_execution_mode = ExecutionMode(execution_mode)
        except ValueError as exc:
            raise ValidationError(
                "execution_mode must be 'manual' or 'auto'"
            ) from exc
        self._ensure_clients()
        try:
            context, state = self._prepare_context(
                spec,
                job_id=job_id,
                execution_mode=requested_execution_mode,
            )
        except VideoLocalizerError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize filesystem/setup failures
            raise VideoLocalizerError(f"Failed to prepare job workspace: {exc}") from exc

        self._initialize_runtime(context)

        start_stage = self._resolve_start_stage(context, resume_from)
        if context.stage == PipelineStage.FAILED and resume_from is not None:
            context.last_error = None
            self._save_checkpoint(context)
        if start_stage == PipelineStage.WAITING_FOR_APPROVAL:
            return self._result(context, action_required="approve_seedance")
        if start_stage == PipelineStage.COMPLETED:
            return self._result(context)
        if start_stage == PipelineStage.ANALYZING and self._recover_analysis_completion(context, state):
            if context.execution_mode == ExecutionMode.MANUAL:
                return self._pause_for_approval(context)
            return self._run_seedance_stage(
                context,
                state,
                run_started=run_started,
            )
        if not skip_preflight and start_stage == PipelineStage.ANALYZING:
            self._logger.info("running startup Preflight")
            report = run_preflight(
                self.config,
                context.spec,
                job_dir=context.job_dir,
                clients={
                    "seedance": self.seedance_client,
                    "uguu": self.uguu_client,
                },
                logger=self._logger,
            )
            preflight_path = context.job_dir / "preflight.json"
            write_json(preflight_path, report.model_dump(mode="json"))
            self._set_artifact(context, "preflight", preflight_path)
            self._save_checkpoint(context)
            try:
                require_preflight(report)
            except PreflightError as exc:
                self._fail(context, PipelineStage.ANALYZING, exc)
                raise

        try:
            self._check_cancel()
            if start_stage == PipelineStage.ANALYZING:
                self._run_analysis_stage(context, state)
                if context.execution_mode == ExecutionMode.MANUAL:
                    return self._pause_for_approval(context)
                self._check_cancel()
                start_stage = PipelineStage.GENERATING_VIDEO

            if start_stage == PipelineStage.GENERATING_VIDEO:
                return self._run_seedance_stage(
                    context,
                    state,
                    run_started=run_started,
                )
            raise ValidationError(f"Unsupported pipeline start stage: {start_stage.value}")
        except PipelineCancelled as exc:
            if context.stage != PipelineStage.FAILED:
                self._fail(context, context.stage, exc, error_code="CANCELLED")
            raise
        except Exception as exc:  # noqa: BLE001 - all failures become internal records
            normalized = (
                exc if isinstance(exc, VideoLocalizerError) else VideoLocalizerError(str(exc))
            )
            if context.stage != PipelineStage.FAILED:
                self._fail(context, context.stage, normalized)
            raise normalized from exc

    def approve_seedance(self, job_id: str) -> PipelineResult:
        """Approve a persisted Doubao package and start a new Seedance attempt."""

        context, state = self._load_existing_context(job_id)
        if context.stage == PipelineStage.ANALYZING:
            if not self._recover_analysis_completion(context, state):
                raise ValidationError("Doubao result is not ready for Seedance approval")
            self._pause_for_approval(context)
        if context.stage != PipelineStage.WAITING_FOR_APPROVAL:
            raise ValidationError("Job is not waiting for Seedance approval")
        if context.approval_status != ApprovalStatus.PENDING:
            raise ValidationError("Job does not have a pending Seedance approval")
        context.approval_status = ApprovalStatus.APPROVED
        context.approved_at = _now_iso()
        context.last_error = None
        self._save_checkpoint(context)
        return self._run_seedance_stage(context, state, run_started=time.monotonic())

    def continue_seedance(self, job_id: str) -> PipelineResult:
        """Continue polling a previously-created Seedance task after restart."""

        context, state = self._load_existing_context(job_id)
        node = self._active_seedance_node(context)
        if node is None:
            latest = self._latest_seedance_node(context)
            final_video = self._artifact_path(context, "final_video", required=False)
            if (
                latest is not None
                and latest.status == NodeExecutionStatus.COMPLETED
                and final_video is not None
                and final_video.is_file()
            ):
                return self.run(
                    context.spec,
                    job_id=job_id,
                    resume_from=PipelineStage.GENERATING_VIDEO,
                    skip_preflight=True,
                    execution_mode=context.execution_mode,
                )
            if (
                context.stage == PipelineStage.GENERATING_VIDEO
                and latest is not None
                and latest.status == NodeExecutionStatus.RUNNING
                and not latest.task_id
            ):
                self._mark_node_interrupted(
                    context,
                    latest,
                    PipelineStage.GENERATING_VIDEO,
                    "Seedance execution was interrupted before a task ID was saved",
                )
                return self.retry_seedance(job_id)
            raise ValidationError("No active Seedance task is available to continue")
        if context.stage not in {PipelineStage.GENERATING_VIDEO, PipelineStage.FAILED}:
            raise ValidationError("Job is not waiting for an active Seedance task")
        context.last_error = None
        context.task_ids["seedance"] = node.task_id
        context.stage = PipelineStage.GENERATING_VIDEO
        context.progress = _PROGRESS[PipelineStage.GENERATING_VIDEO]
        self._save_checkpoint(context)
        return self._run_seedance_stage(
            context,
            state,
            run_started=time.monotonic(),
            continue_existing=True,
        )

    def retry_seedance(self, job_id: str) -> PipelineResult:
        """Create a fresh Seedance task while reusing the saved Doubao package."""

        context, state = self._load_existing_context(job_id)
        failed_stage = (context.last_error or {}).get("stage")
        if context.stage != PipelineStage.FAILED or failed_stage != PipelineStage.GENERATING_VIDEO.value:
            raise ValidationError("Only a failed Seedance job can be retried")
        node = self._latest_seedance_node(context)
        if node is not None and node.status == NodeExecutionStatus.RUNNING and node.task_id:
            raise ValidationError("Seedance task is still active; continue waiting instead")
        if not isinstance(state.get("package"), LocalizationPackage):
            raise ValidationError("Saved Doubao localization package is missing")
        context.last_error = None
        context.task_ids.pop("seedance", None)
        context.request_ids.pop("seedance", None)
        state.pop("final_video", None)
        return self._run_seedance_stage(
            context,
            state,
            run_started=time.monotonic(),
            force_new_task=True,
        )

    def resume_failed(self, job_id: str, *, spec: JobSpec | None = None) -> PipelineResult:
        """Backward-compatible failure recovery dispatcher used by the GUI."""

        context, state = self._load_existing_context(job_id)
        if spec is not None and spec.target_locale != context.spec.target_locale:
            raise ValidationError("Retry spec target locale does not match the checkpoint")
        failed_stage = (context.last_error or {}).get("stage")
        if context.stage == PipelineStage.ANALYZING:
            if self._recover_analysis_completion(context, state):
                if context.execution_mode == ExecutionMode.MANUAL:
                    return self._pause_for_approval(context)
                return self._run_seedance_stage(
                    context,
                    state,
                    run_started=time.monotonic(),
                )
            node = self._latest_node(context, _DOUBAO_NODE)
            if node is not None and node.status == NodeExecutionStatus.RUNNING:
                self._mark_node_interrupted(
                    context,
                    node,
                    PipelineStage.ANALYZING,
                    "Doubao execution was interrupted before a result was saved",
                )
            return self.run(
                context.spec,
                job_id=job_id,
                resume_from=PipelineStage.ANALYZING,
                execution_mode=context.execution_mode,
            )
        if failed_stage == PipelineStage.GENERATING_VIDEO.value:
            node = self._active_seedance_node(context)
            if node is not None and node.task_id:
                return self.continue_seedance(job_id)
            latest = self._latest_seedance_node(context)
            if latest is not None and latest.status == NodeExecutionStatus.RUNNING:
                self._mark_node_interrupted(
                    context,
                    latest,
                    PipelineStage.GENERATING_VIDEO,
                    "Seedance execution was interrupted before a task ID was saved",
                )
            return self.retry_seedance(job_id)
        if context.stage == PipelineStage.GENERATING_VIDEO:
            latest = self._latest_seedance_node(context)
            if latest is not None and latest.status == NodeExecutionStatus.RUNNING:
                if latest.task_id:
                    return self.continue_seedance(job_id)
                self._mark_node_interrupted(
                    context,
                    latest,
                    PipelineStage.GENERATING_VIDEO,
                    "Seedance execution was interrupted before a task ID was saved",
                )
                return self.retry_seedance(job_id)
            final_video = self._artifact_path(context, "final_video", required=False)
            if (
                latest is None
                or latest.status != NodeExecutionStatus.COMPLETED
                or final_video is None
                or not final_video.is_file()
            ):
                record = ErrorRecord(
                    stage=PipelineStage.GENERATING_VIDEO.value,
                    message="Seedance execution was interrupted before its attempt was completed",
                    error_code="INTERRUPTED",
                    retryable=False,
                ).as_dict()
                context.stage = PipelineStage.FAILED
                context.last_error = record
                context.task_ids.pop("seedance", None)
                context.request_ids.pop("seedance", None)
                self._save_checkpoint(context)
                return self.retry_seedance(job_id)
        if failed_stage == PipelineStage.ANALYZING.value:
            return self.run(
                context.spec,
                job_id=job_id,
                resume_from=PipelineStage.ANALYZING,
                execution_mode=context.execution_mode,
            )
        raise ValidationError("Checkpoint does not identify a resumable v4 stage")

    def _prepare_context(
        self,
        spec: JobSpec,
        *,
        job_id: str | None,
        execution_mode: ExecutionMode | str = ExecutionMode.MANUAL,
    ) -> tuple[JobContext, dict[str, Any]]:
        resolved_id = job_id or spec.job_id or new_job_id()
        if (
            not resolved_id
            or resolved_id in {".", ".."}
            or Path(resolved_id).name != resolved_id
        ):
            raise ValidationError("Invalid job ID")
        job_dir = self.config.work_dir / resolved_id
        checkpoint_path = job_dir / "checkpoint.json"
        if job_id and checkpoint_path.is_file():
            raw = read_json(checkpoint_path)
            self._assert_checkpoint_compatible(raw, resolved_id)
            context = JobContext.model_validate(raw)
            if (
                spec.target_language != context.spec.target_language
                or spec.target_region != context.spec.target_region
                or spec.target_locale != context.spec.target_locale
            ):
                raise ValidationError(
                    "Job target settings do not match the existing v4 checkpoint; start a new job"
                )
            self._assert_cache_key_current(context, spec)
            state = self._hydrate_state(context)
            return context, state

        context = JobContext(
            pipeline_version=PIPELINE_VERSION,
            job_id=resolved_id,
            job_dir=job_dir.resolve(),
            spec=spec.model_copy(update={"job_id": resolved_id}),
            execution_mode=ExecutionMode(execution_mode),
            approval_status=ApprovalStatus.NOT_REQUIRED,
            created_at=_now_iso(),
            updated_at=_now_iso(),
        )
        for relative in (
            "input",
            "input/refs/characters",
            "input/refs/scenes",
            "json/raw",
            "output",
        ):
            (context.job_dir / relative).mkdir(parents=True, exist_ok=True)

        if not spec.input_video.is_file():
            raise ValidationError(f"Input video does not exist: {spec.input_video}")
        source_copy = context.job_dir / "input" / spec.input_video.name
        shutil.copy2(spec.input_video, source_copy)
        self._set_artifact(context, "source_video", source_copy)

        references: dict[str, list[str]] = {"character": [], "scene": []}
        for kind, paths, directory in (
            ("character", spec.character_refs, context.job_dir / "input/refs/characters"),
            ("scene", spec.scene_refs, context.job_dir / "input/refs/scenes"),
        ):
            for index, path in enumerate(paths, start=1):
                if not path.is_file():
                    raise ValidationError(f"Reference file does not exist: {path}")
                destination = directory / f"{kind}_{index:03d}_{path.name}"
                shutil.copy2(path, destination)
                references[kind].append(str(destination))
        references_path = context.job_dir / "input/references.json"
        write_json(references_path, references)
        self._set_artifact(context, "references", references_path)
        context.cache_key = self._build_cache_key(source_copy, spec)
        self._save_checkpoint(context)
        return context, {"source_video": source_copy, "references": references}

    def _hydrate_state(self, context: JobContext) -> dict[str, Any]:
        state: dict[str, Any] = {
            "source_video": self._artifact_path(context, "source_video"),
            "references": {"character": [], "scene": []},
        }
        references_path = self._artifact_path(context, "references", required=False)
        if references_path and references_path.is_file():
            state["references"] = read_json(references_path)

        source_info_path = self._artifact_path(context, "source_info", required=False)
        if source_info_path and source_info_path.is_file():
            source_info = read_json(source_info_path)
            state["source_info"] = source_info
            duration_value = source_info.get("duration")
            if duration_value is None and isinstance(source_info.get("format"), dict):
                duration_value = source_info["format"].get("duration")
            if duration_value is None:
                raise ValidationError("checkpoint source metadata has no duration")
            state["source_duration"] = float(duration_value)

        assets_path = self._artifact_path(context, "assets", required=False)
        if assets_path and assets_path.is_file():
            assets = [UploadedAsset.model_validate(item) for item in read_json(assets_path)]
            state["assets"] = assets
            for asset in assets:
                if asset.kind == "source_video":
                    state["source_video_asset"] = asset

        package_path = self._artifact_path(context, "localization_package", required=False)
        if package_path is None:
            recovered_package_path = context.job_dir / "json/localization_package.json"
            if recovered_package_path.is_file():
                package_path = recovered_package_path
                self._set_artifact(context, "localization_package", recovered_package_path)
        if package_path and package_path.is_file():
            state["package"] = LocalizationPackage.model_validate(read_json(package_path))

        content_path = self._artifact_path(context, "seedance_content", required=False)
        if content_path and content_path.is_file():
            state["seedance_content"] = read_json(content_path)

        result_path = self._artifact_path(context, "seedance_result", required=False)
        if result_path and result_path.is_file():
            state["seedance_result"] = read_json(result_path)

        final_info_path = self._artifact_path(context, "final_info", required=False)
        if final_info_path and final_info_path.is_file():
            state["final_info"] = read_json(final_info_path)

        final_video = self._artifact_path(context, "final_video", required=False)
        if final_video and final_video.is_file():
            state["final_video"] = final_video
        return state

    def _assert_checkpoint_compatible(self, value: Any, job_id: str) -> None:
        if not isinstance(value, dict):
            raise ValidationError(f"Invalid checkpoint for {job_id}: root must be an object")
        if value.get("job_id") != job_id:
            raise ValidationError(
                f"Checkpoint for {job_id} has a mismatched job ID; 需要新建任务 (start a new job)"
            )
        checkpoint_job_dir = value.get("job_dir")
        if not isinstance(checkpoint_job_dir, str):
            raise ValidationError(
                f"Checkpoint for {job_id} has no valid job directory; 需要新建任务 (start a new job)"
            )
        if Path(checkpoint_job_dir).expanduser().resolve() != (
            self.config.work_dir / job_id
        ).resolve():
            raise ValidationError(
                f"Checkpoint for {job_id} points outside its job directory; "
                "需要新建任务 (start a new job)"
            )
        if value.get("pipeline_version") != PIPELINE_VERSION:
            raise ValidationError(
                f"Checkpoint for {job_id} belongs to an older pipeline; 需要新建任务 (start a new job)"
            )
        valid_stages = {stage.value for stage in PipelineStage}
        if value.get("stage") not in valid_stages:
            raise ValidationError(
                f"Checkpoint for {job_id} contains an unsupported pipeline stage; 需要新建任务 (start a new job)"
            )
        artifacts = value.get("artifacts")
        if not isinstance(artifacts, dict) or set(artifacts) - _V4_ARTIFACTS:
            raise ValidationError(
                f"Checkpoint for {job_id} contains unsupported artifact state; 需要新建任务 (start a new job)"
            )
        cache_key = value.get("cache_key")
        if not isinstance(cache_key, dict) or set(cache_key) != _V4_CACHE_KEY_FIELDS:
            raise ValidationError(
                f"Checkpoint for {job_id} has no valid v4 cache key; 需要新建任务 (start a new job)"
            )
        spec = value.get("spec")
        if not isinstance(spec, dict) or set(spec) - set(JobSpec.model_fields):
            raise ValidationError(
                f"Checkpoint for {job_id} contains legacy job fields; 需要新建任务 (start a new job)"
            )
        for field in ("task_ids", "request_ids"):
            entries = value.get(field)
            if not isinstance(entries, dict) or set(entries) - _V4_TASK_KEYS:
                raise ValidationError(
                    f"Checkpoint for {job_id} contains unsupported provider state; 需要新建任务 (start a new job)"
                )

    def _assert_cache_key_current(self, context: JobContext, spec: JobSpec) -> None:
        source_copy = self._artifact_path(context, "source_video")
        expected = self._build_cache_key(source_copy, spec)
        if context.cache_key != expected:
            raise ValidationError(
                "Existing checkpoint cache key does not match the current v4 configuration; "
                "需要新建任务 (start a new job)"
            )
        if spec.input_video.is_file():
            requested_source = self._build_cache_key(spec.input_video, spec)
            if requested_source["source_video_hash"] != context.cache_key["source_video_hash"]:
                raise ValidationError(
                    "Input video does not match the existing v4 checkpoint; 需要新建任务 (start a new job)"
                )

    def _resolve_start_stage(
        self,
        context: JobContext,
        requested: PipelineStage | str | None,
    ) -> PipelineStage:
        if requested is not None:
            stage = PipelineStage(requested)
            if stage not in PIPELINE_STAGES:
                raise ValidationError(f"Stage {stage.value} cannot be used as a pipeline start")
            return stage
        if context.stage == PipelineStage.COMPLETED:
            return PipelineStage.COMPLETED
        if context.stage in PIPELINE_STAGES:
            return context.stage
        return PipelineStage.ANALYZING

    def _initialize_runtime(self, context: JobContext) -> None:
        self._logger = JobLogger(
            context.job_dir / "job.log",
            callback=lambda event: self._emit_log(context, event),
        )
        self._logger.info(
            "localization job started",
            job_id=context.job_id,
            target_locale=context.spec.target_locale,
            pipeline_version=PIPELINE_VERSION,
            execution_mode=context.execution_mode.value,
        )
        for client in (self.ark_client, self.uguu_client, self.seedance_client):
            if hasattr(client, "logger"):
                client.logger = self._logger

    def _load_existing_context(self, job_id: str) -> tuple[JobContext, dict[str, Any]]:
        self._ensure_clients()
        raw = self.history_store.load_raw(job_id)
        self._assert_checkpoint_compatible(raw, job_id)
        try:
            context = JobContext.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - normalize checkpoint errors
            raise ValidationError(f"Invalid checkpoint for {job_id}: {exc}") from exc
        self._assert_cache_key_current(context, context.spec)
        state = self._hydrate_state(context)
        self._initialize_runtime(context)
        return context, state

    def _result(
        self,
        context: JobContext,
        *,
        action_required: str | None = None,
    ) -> PipelineResult:
        output_path = self._artifact_path(context, "final_video", required=False)
        if output_path is not None and not output_path.is_file():
            output_path = None
        package_path = self._artifact_path(context, "localization_package", required=False)
        return PipelineResult(
            job_id=context.job_id,
            stage=context.stage,
            output_path=output_path,
            package_path=package_path if package_path and package_path.is_file() else None,
            action_required=action_required,
        )

    def _pause_for_approval(self, context: JobContext) -> PipelineResult:
        context.approval_status = ApprovalStatus.PENDING
        self._set_stage(context, PipelineStage.WAITING_FOR_APPROVAL)
        package_path = self._artifact_path(context, "localization_package", required=False)
        self._emit(
            context,
            "approval_required",
            "Doubao analysis completed; review the package before Seedance",
            package_path=str(package_path) if package_path else None,
        )
        self._save_checkpoint(context)
        return self._result(context, action_required="approve_seedance")

    def _recover_analysis_completion(
        self,
        context: JobContext,
        state: dict[str, Any],
    ) -> bool:
        """Promote a durably-written package without calling Doubao again."""

        package = state.get("package")
        node = self._latest_node(context, _DOUBAO_NODE)
        if not isinstance(package, LocalizationPackage) or node is None:
            return False
        if node.status == NodeExecutionStatus.RUNNING:
            package_path = self._artifact_path(context, "localization_package", required=False)
            self._finish_node(
                context,
                node,
                NodeExecutionStatus.COMPLETED,
                output_artifacts=[
                    self._path_reference(context, package_path)
                    if package_path
                    else "json/localization_package.json"
                ],
            )
        elif node.status != NodeExecutionStatus.COMPLETED:
            return False
        context.metrics.setdefault("speaker_count", len(package.speakers))
        context.metrics.setdefault("dialogue_count", len(package.dialogues))
        self._save_checkpoint(context)
        return True

    def _latest_node(self, context: JobContext, node_name: str) -> NodeExecution | None:
        return next(
            (item for item in reversed(context.node_executions) if item.node == node_name),
            None,
        )

    def _latest_seedance_node(self, context: JobContext) -> NodeExecution | None:
        return self._latest_node(context, _SEEDANCE_NODE)

    def _active_seedance_node(self, context: JobContext) -> NodeExecution | None:
        node = self._latest_seedance_node(context)
        if (
            node is None
            or node.status != NodeExecutionStatus.RUNNING
            or not node.task_id
        ):
            return None
        return node

    def _begin_node(
        self,
        context: JobContext,
        node_name: str,
        *,
        input_artifacts: list[str],
    ) -> NodeExecution:
        previous_attempts = [
            item.attempt
            for item in context.node_executions
            if item.node == node_name
        ]
        node = NodeExecution(
            node=node_name,
            attempt=max(previous_attempts, default=0) + 1,
            status=NodeExecutionStatus.RUNNING,
            started_at=_now_iso(),
            input_artifacts=input_artifacts,
        )
        context.node_executions.append(node)
        self._save_checkpoint(context)
        self._emit(
            context,
            "node_started",
            f"{node_name} node started",
            node=node_name,
            attempt=node.attempt,
        )
        return node

    def _finish_node(
        self,
        context: JobContext,
        node: NodeExecution,
        status: NodeExecutionStatus,
        *,
        error: dict[str, Any] | None = None,
        output_artifacts: list[str] | None = None,
    ) -> None:
        node.status = status
        node.finished_at = _now_iso()
        node.error = error
        for path in output_artifacts or []:
            if path not in node.output_artifacts:
                node.output_artifacts.append(path)
        self._save_checkpoint(context)
        self._emit(
            context,
            "node_completed" if status == NodeExecutionStatus.COMPLETED else "node_failed",
            f"{node.node} node {status.value}",
            node=node.node,
            attempt=node.attempt,
            status=status.value,
        )

    def _mark_node_interrupted(
        self,
        context: JobContext,
        node: NodeExecution,
        stage: PipelineStage,
        message: str,
    ) -> None:
        record = ErrorRecord(
            stage=stage.value,
            message=message,
            error_code="INTERRUPTED",
            retryable=False,
        ).as_dict()
        node.status = NodeExecutionStatus.FAILED
        node.finished_at = _now_iso()
        node.error = record
        context.stage = PipelineStage.FAILED
        context.last_error = record
        context.task_ids.pop("seedance", None)
        context.request_ids.pop("seedance", None)
        self._save_checkpoint(context)

    def _record_provider_call(
        self,
        context: JobContext,
        node: NodeExecution,
        detail: dict[str, Any],
    ) -> None:
        status = NodeExecutionStatus(str(detail.get("status", "failed")))
        request_id = detail.get("request_id")
        raw_response_path = detail.get("raw_response_path")
        call = ProviderCall(
            request_id=str(request_id) if request_id else None,
            status=status,
            started_at=str(detail.get("started_at") or _now_iso()),
            finished_at=(
                str(detail["finished_at"])
                if detail.get("finished_at")
                else _now_iso()
            ),
            raw_response_path=(str(raw_response_path) if raw_response_path else None),
            error=detail.get("error") if isinstance(detail.get("error"), dict) else None,
        )
        node.provider_calls.append(call)
        if request_id:
            request_value = str(request_id)
            if request_value not in node.request_ids:
                node.request_ids.append(request_value)
        self._save_checkpoint(context)
        self._emit(
            context,
            "provider_call",
            f"{node.node} provider call {status.value}",
            node=node.node,
            attempt=node.attempt,
            request_id=request_id,
            raw_response_path=raw_response_path,
        )

    def _path_reference(self, context: JobContext, path: Path) -> str:
        path = Path(path)
        try:
            return str(path.resolve().relative_to(context.job_dir.resolve()))
        except ValueError:
            return str(path)

    def _run_analysis_stage(self, context: JobContext, state: dict[str, Any]) -> None:
        self._set_stage(context, PipelineStage.ANALYZING)
        stage_started = time.monotonic()
        node = self._begin_node(
            context,
            _DOUBAO_NODE,
            input_artifacts=[self._path_reference(context, state["source_video"])],
        )
        state["_doubao_node"] = node
        try:
            self._analyze_video(context, state)
        except Exception as exc:  # noqa: BLE001 - node and pipeline both retain the error
            self._finish_node(
                context,
                node,
                NodeExecutionStatus.FAILED,
                error=self._error_record(PipelineStage.ANALYZING, exc).as_dict(),
            )
            raise
        package_path = self._artifact_path(context, "localization_package")
        self._finish_node(
            context,
            node,
            NodeExecutionStatus.COMPLETED,
            output_artifacts=[
                self._path_reference(context, package_path)
                if package_path
                else "json/localization_package.json"
            ],
        )
        elapsed = round(time.monotonic() - stage_started, 3)
        context.metrics["analysis_duration"] = elapsed
        self._logger.info(
            "pipeline stage completed",
            job_id=context.job_id,
            target_locale=context.spec.target_locale,
            source_video_duration=context.metrics.get("source_video_duration"),
            stage=PipelineStage.ANALYZING.value,
            stage_duration_seconds=elapsed,
        )
        self._save_checkpoint(context)

    def _run_seedance_stage(
        self,
        context: JobContext,
        state: dict[str, Any],
        *,
        run_started: float,
        force_new_task: bool = False,
        continue_existing: bool = False,
    ) -> PipelineResult:
        self._set_stage(context, PipelineStage.GENERATING_VIDEO)
        stage_started = time.monotonic()
        try:
            self._generate_video(
                context,
                state,
                force_new_task=force_new_task,
                continue_existing=continue_existing,
            )
            elapsed = round(time.monotonic() - stage_started, 3)
            context.metrics["seedance_duration"] = elapsed
            context.metrics["total_duration"] = round(time.monotonic() - run_started, 3)
            context.last_error = None
            context.stage = PipelineStage.COMPLETED
            context.progress = 100
            self._save_checkpoint(context)
            self._logger.info(
                "localization job completed",
                job_id=context.job_id,
                target_locale=context.spec.target_locale,
                source_video_duration=context.metrics.get("source_video_duration"),
                speaker_count=context.metrics.get("speaker_count", 0),
                dialogue_count=context.metrics.get("dialogue_count", 0),
                total_duration_seconds=context.metrics["total_duration"],
            )
            output_path = self._artifact_path(context, "final_video")
            self._emit(
                context,
                "completed",
                "Localization completed",
                output=str(output_path) if output_path else None,
            )
            return self._result(context)
        except PipelineCancelled as exc:
            self._fail(context, PipelineStage.GENERATING_VIDEO, exc, error_code="CANCELLED")
            raise
        except Exception as exc:  # noqa: BLE001 - all failures become internal records
            normalized = (
                exc if isinstance(exc, VideoLocalizerError) else VideoLocalizerError(str(exc))
            )
            self._fail(context, PipelineStage.GENERATING_VIDEO, normalized)
            raise normalized from exc

    def _set_stage(self, context: JobContext, stage: PipelineStage) -> None:
        context.stage = stage
        context.progress = _PROGRESS[stage]
        self._emit(context, "stage", f"Stage: {stage.value}")
        if self._logger:
            self._logger.info("pipeline stage started", stage=stage.value)
        self._save_checkpoint(context)

    def _emit(
        self,
        context: JobContext,
        event_type: str,
        message: str,
        **metadata: Any,
    ) -> None:
        event = PipelineEvent(
            event_type=event_type,
            job_id=context.job_id,
            stage=context.stage,
            progress=context.progress,
            message=message,
            metadata=metadata,
        ).model_dump(mode="json")
        if self.event_callback is not None:
            self.event_callback(event)

    def _emit_log(self, context: JobContext, log_event: dict[str, Any]) -> None:
        attempt = log_event.get("attempt")
        if isinstance(attempt, int):
            context.retry_counts[context.stage.value] = max(
                context.retry_counts.get(context.stage.value, 0),
                attempt,
            )
        metadata = {
            key: value
            for key, value in log_event.items()
            if key not in {"message", "timestamp", "level"}
        }
        self._emit(
            context,
            "log",
            str(log_event.get("message", "")),
            level=log_event.get("level"),
            **metadata,
        )

    def _save_checkpoint(self, context: JobContext) -> None:
        context.updated_at = _now_iso()
        write_json(context.job_dir / "checkpoint.json", context.model_dump(mode="json"))
        try:
            self.history_store.upsert(context)
        except Exception as exc:  # noqa: BLE001 - index is rebuildable and must not fail a job
            if self._logger:
                self._logger.warning("execution history index update failed", error=str(exc))

    def _set_artifact(self, context: JobContext, name: str, path: Path) -> None:
        path = Path(path)
        try:
            context.artifacts[name] = str(path.resolve().relative_to(context.job_dir.resolve()))
        except ValueError:
            context.artifacts[name] = str(path)

    def _artifact_path(
        self,
        context: JobContext,
        name: str,
        *,
        required: bool = True,
    ) -> Path | None:
        value = context.artifacts.get(name)
        if not value:
            if required:
                raise ValidationError(f"checkpoint artifact is missing: {name}")
            return None
        path = Path(value)
        return path if path.is_absolute() else context.job_dir / path

    def _raw_dir(self, context: JobContext) -> Path:
        return context.job_dir / "json/raw"

    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise PipelineCancelled("Cancellation requested")

    def _record_task(
        self,
        context: JobContext,
        key: str,
        task_id: str | None,
        request_id: str | None,
    ) -> None:
        if task_id:
            context.task_ids[key] = task_id
        if request_id:
            context.request_ids[key] = request_id
        if task_id or request_id:
            self._emit(
                context,
                "task",
                f"{key} task submitted",
                task_id=task_id,
                request_id=request_id,
            )

    def _fail(
        self,
        context: JobContext,
        stage: PipelineStage,
        exc: Exception,
        *,
        error_code: str | None = None,
    ) -> None:
        context.stage = PipelineStage.FAILED
        record = self._error_record(stage, exc, error_code=error_code)
        context.last_error = record.as_dict()
        self._save_checkpoint(context)
        self._emit(context, "error", record.message, error=record.as_dict())
        if self._logger:
            self._logger.error(record.message, stage=stage.value, error_code=record.error_code)

    @staticmethod
    def _error_record(
        stage: PipelineStage,
        exc: Exception,
        *,
        error_code: str | None = None,
    ) -> ErrorRecord:
        if isinstance(exc, ProviderError):
            record = exc.as_record(stage.value)
            if error_code:
                record.error_code = error_code
            return record
        if isinstance(exc, MediaCommandError):
            return ErrorRecord(
                stage=stage.value,
                message=str(exc),
                provider="ffprobe",
                error_code=error_code or "MEDIA_COMMAND_FAILED",
            )
        return ErrorRecord(
            stage=stage.value,
            message=str(exc),
            error_code=error_code or exc.__class__.__name__,
        )

    def _analyze_video(self, context: JobContext, state: dict[str, Any]) -> None:
        self._check_cancel()
        source = state["source_video"]
        info = ffprobe.probe(
            source,
            ffprobe_bin=self.config.ffprobe_bin,
            timeout=self.config.http_timeout,
        )
        if not info.has_video:
            raise ValidationError("input video must contain a video stream")
        validate_duration(info.duration, self.config.seedance_max_duration)
        state["source_info"] = info.raw
        state["source_duration"] = info.duration
        context.metrics["source_video_duration"] = round(info.duration, 3)
        source_info_path = context.job_dir / "json/source_info.json"
        write_json(source_info_path, info.raw)
        self._set_artifact(context, "source_info", source_info_path)

        source_asset = self.uguu_client.upload(
            source,
            kind="source_video",
            raw_dir=self._raw_dir(context),
        )
        upload_items: list[tuple[Path, str]] = []
        for path in state["references"].get("character", []):
            upload_items.append((Path(path), "character_reference"))
        for path in state["references"].get("scene", []):
            upload_items.append((Path(path), "scene_reference"))
        reference_assets = self.uguu_client.upload_many(
            upload_items,
            raw_dir=self._raw_dir(context),
        )
        assets = [source_asset, *reference_assets]
        state["source_video_asset"] = source_asset
        state["assets"] = assets
        assets_path = context.job_dir / "json/assets.json"
        write_json(assets_path, [asset.model_dump(mode="json") for asset in assets])
        self._set_artifact(context, "assets", assets_path)

        package = analyze_video(
            self.ark_client,
            source_asset.remote_url,
            target_language=context.spec.target_language,
            target_region=context.spec.target_region,
            target_locale=context.spec.target_locale,
            duration_seconds=info.duration,
            raw_dir=self._raw_dir(context),
            logger=self._logger,
            attempt_callback=(
                lambda detail: self._record_provider_call(
                    context,
                    state["_doubao_node"],
                    detail,
                )
                if isinstance(state.get("_doubao_node"), NodeExecution)
                else None
            ),
        )
        state["package"] = package
        context.metrics["speaker_count"] = len(package.speakers)
        context.metrics["dialogue_count"] = len(package.dialogues)
        if self._logger:
            self._logger.info(
                "video analysis completed",
                job_id=context.job_id,
                target_locale=context.spec.target_locale,
                source_video_duration=round(info.duration, 3),
                speaker_count=len(package.speakers),
                dialogue_count=len(package.dialogues),
            )
        package_path = context.job_dir / "json/localization_package.json"
        write_json(package_path, package.model_dump(mode="json"))
        self._set_artifact(context, "localization_package", package_path)

    def _generate_video(
        self,
        context: JobContext,
        state: dict[str, Any],
        *,
        force_new_task: bool = False,
        continue_existing: bool = False,
    ) -> None:
        self._check_cancel()
        package = state.get("package")
        if not isinstance(package, LocalizationPackage):
            raise ValidationError("localization package is missing")

        node = None if force_new_task else self._active_seedance_node(context)
        latest_node = None if force_new_task else self._latest_seedance_node(context)
        if (
            not force_new_task
            and latest_node is not None
            and latest_node.status == NodeExecutionStatus.COMPLETED
            and "final_video" in state
            and state["final_video"].is_file()
        ):
            return
        if node is not None and node.task_id:
            continue_existing = True
        if continue_existing:
            if node is None or not node.task_id:
                raise ValidationError("checkpoint has no active Seedance task")
            try:
                self._wait_and_finalize_seedance(context, state, node, node.task_id)
            except PipelineCancelled:
                node.error = self._error_record(
                    PipelineStage.GENERATING_VIDEO,
                    PipelineCancelled("Cancellation requested while waiting for Seedance"),
                    error_code="CANCELLED",
                ).as_dict()
                self._save_checkpoint(context)
                raise
            except ProviderError as exc:
                self._handle_seedance_provider_error(context, node, exc)
                raise
            except Exception as exc:  # noqa: BLE001 - preserve the attempt failure
                self._finish_seedance_node_with_error(context, node, exc)
                raise
            return

        if node is None:
            assets = state.get("assets")
            if not isinstance(assets, list) or any(
                not isinstance(asset, UploadedAsset) for asset in assets
            ):
                raise ValidationError("checkpoint is missing uploaded assets")
            node = self._begin_node(
                context,
                _SEEDANCE_NODE,
                input_artifacts=[
                    self._path_reference(context, state["source_video"]),
                    self._path_reference(
                        context,
                        self._artifact_path(context, "localization_package"),
                    ),
                    self._path_reference(
                        context,
                        self._artifact_path(context, "references"),
                    ),
                ],
            )

        try:
            assets = state.get("assets")
            if not isinstance(assets, list) or any(
                not isinstance(asset, UploadedAsset) for asset in assets
            ):
                raise ValidationError("checkpoint is missing uploaded assets")
            refreshed_assets = [self._refresh_asset(context, asset) for asset in assets]
            state["assets"] = refreshed_assets
            self._persist_assets(context, state)
            source_asset = next(
                (asset for asset in refreshed_assets if asset.kind == "source_video"),
                None,
            )
            if source_asset is None:
                raise ValidationError("checkpoint is missing the source video asset")
            reference_assets = [
                asset
                for asset in refreshed_assets
                if asset.kind in {"character_reference", "scene_reference"}
            ]

            attempt_dir = self._seedance_attempt_dir(context, node)
            content = build_seedance_content(
                source_asset.remote_url,
                package,
                reference_assets,
                context.spec,
            )
            state["seedance_content"] = content
            content_path = attempt_dir / "content.json"
            write_json(content_path, content)
            self._set_artifact(context, "seedance_content", content_path)
            self._append_node_output(context, node, content_path)

            task = self.seedance_client.create_task(
                content,
                raw_dir=self._raw_dir(context),
            )
            task_id = getattr(task, "task_id", None)
            request_id = getattr(task, "request_id", None)
            if not task_id:
                raise ProviderError(
                    "Seedance create response has no task ID",
                    provider="seedance",
                    error_code="TASK_ID_MISSING",
                    retryable=False,
                )
            node.task_id = str(task_id)
            self._record_provider_call(
                context,
                node,
                {
                    "status": "completed",
                    "request_id": request_id,
                    "raw_response_path": (
                        str(getattr(task, "raw_path", None))
                        if getattr(task, "raw_path", None)
                        else None
                    ),
                    "started_at": _now_iso(),
                    "finished_at": _now_iso(),
                },
            )
            self._record_task(context, "seedance", str(task_id), request_id)
            # Persist the task ID before polling so an application exit can
            # continue the same cloud task without creating a duplicate.
            self._save_checkpoint(context)

            self._wait_and_finalize_seedance(context, state, node, str(task_id))
        except PipelineCancelled:
            if node.task_id:
                node.error = self._error_record(
                    PipelineStage.GENERATING_VIDEO,
                    PipelineCancelled("Cancellation requested while waiting for Seedance"),
                    error_code="CANCELLED",
                ).as_dict()
                self._save_checkpoint(context)
            else:
                self._finish_seedance_node_with_error(
                    context,
                    node,
                    PipelineCancelled("Cancellation requested"),
                )
            raise
        except ProviderError as exc:
            self._handle_seedance_provider_error(context, node, exc)
            raise
        except Exception as exc:  # noqa: BLE001 - preserve all local failures
            self._finish_seedance_node_with_error(context, node, exc)
            raise

    def _seedance_attempt_dir(self, context: JobContext, node: NodeExecution) -> Path:
        path = context.job_dir / "json" / "nodes" / "seedance" / f"attempt_{node.attempt:03d}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _append_node_output(
        self,
        context: JobContext,
        node: NodeExecution,
        path: Path,
    ) -> None:
        reference = self._path_reference(context, path)
        if reference not in node.output_artifacts:
            node.output_artifacts.append(reference)
        self._save_checkpoint(context)

    def _wait_and_finalize_seedance(
        self,
        context: JobContext,
        state: dict[str, Any],
        node: NodeExecution,
        task_id: str,
    ) -> None:
        response = self.seedance_client.wait_task(
            task_id,
            raw_dir=self._raw_dir(context),
            cancel_event=self.cancel_event,
        )
        self._record_provider_call(
            context,
            node,
            {
                "status": "completed",
                "request_id": getattr(response, "request_id", None),
                "raw_response_path": (
                    str(getattr(response, "raw_path", None))
                    if getattr(response, "raw_path", None)
                    else None
                ),
                "started_at": _now_iso(),
                "finished_at": _now_iso(),
            },
        )
        data = response.data if hasattr(response, "data") else response
        content = data.get("content") if isinstance(data, dict) else None
        video_url = content.get("video_url") if isinstance(content, dict) else None
        if not video_url:
            raise ProviderError(
                "Seedance succeeded without content.video_url",
                provider="seedance",
                error_code="VIDEO_URL_MISSING",
                retryable=False,
                payload=data,
            )

        attempt_dir = self._seedance_attempt_dir(context, node)
        result_path = attempt_dir / "result.json"
        write_json(result_path, data)
        self._append_node_output(context, node, result_path)
        self._set_artifact(context, "seedance_result", result_path)

        attempt_video = attempt_dir / "final.mp4"
        download(
            str(video_url),
            attempt_video,
            timeout=self.config.http_timeout,
            attempts=self.config.max_retries,
        )
        self._append_node_output(context, node, attempt_video)
        if not attempt_video.is_file() or attempt_video.stat().st_size == 0:
            raise ProviderError(
                "Seedance produced no final video file",
                provider="seedance",
                error_code="EMPTY_VIDEO_RESULT",
                retryable=False,
                payload=data,
            )
        final_info = ffprobe.probe(
            attempt_video,
            ffprobe_bin=self.config.ffprobe_bin,
            timeout=self.config.http_timeout,
        )
        if not final_info.has_video:
            raise ValidationError("Seedance output has no video stream")
        if not final_info.has_audio:
            raise ValidationError("Seedance output has no generated audio stream")
        if final_info.duration <= 0:
            raise ValidationError("Seedance output has no positive duration")

        attempt_info_path = attempt_dir / "final_info.json"
        write_json(attempt_info_path, final_info.raw)
        self._append_node_output(context, node, attempt_info_path)
        stable_info_path = context.job_dir / "json/final_info.json"
        write_json(stable_info_path, final_info.raw)
        self._set_artifact(context, "final_info", stable_info_path)

        final_video = context.job_dir / f"output/final_{context.spec.target_locale}.mp4"
        shutil.copy2(attempt_video, final_video)
        state["final_video"] = final_video
        state["final_info"] = final_info.raw
        self._set_artifact(context, "final_video", final_video)
        self._append_node_output(context, node, final_video)
        self._finish_node(
            context,
            node,
            NodeExecutionStatus.COMPLETED,
            output_artifacts=[
                self._path_reference(context, result_path),
                self._path_reference(context, attempt_video),
                self._path_reference(context, final_video),
            ],
        )

    def _handle_seedance_provider_error(
        self,
        context: JobContext,
        node: NodeExecution,
        exc: ProviderError,
    ) -> None:
        record = self._error_record(PipelineStage.GENERATING_VIDEO, exc).as_dict()
        failure_path = self._seedance_attempt_dir(context, node) / "failure.json"
        write_json(
            failure_path,
            {
                "error": record,
                "provider_payload": exc.payload,
            },
        )
        self._append_node_output(context, node, failure_path)
        self._record_provider_call(
            context,
            node,
            {
                "status": "failed",
                "request_id": exc.request_id,
                "raw_response_path": exc.raw_response_path,
                "started_at": _now_iso(),
                "finished_at": _now_iso(),
                "error": {"message": str(exc), "error_code": exc.error_code},
            },
        )
        if not node.task_id or exc.error_code in _SEEDANCE_TERMINAL_ERRORS:
            self._finish_node(
                context,
                node,
                NodeExecutionStatus.FAILED,
                error=record,
            )
            context.task_ids.pop("seedance", None)
            context.request_ids.pop("seedance", None)
        else:
            # A timeout or transient polling failure may leave the cloud task
            # alive; preserve the task ID so the user can continue waiting.
            node.error = record
            self._save_checkpoint(context)

    def _finish_seedance_node_with_error(
        self,
        context: JobContext,
        node: NodeExecution,
        exc: Exception,
    ) -> None:
        record = self._error_record(PipelineStage.GENERATING_VIDEO, exc).as_dict()
        failure_path = self._seedance_attempt_dir(context, node) / "failure.json"
        write_json(
            failure_path,
            {
                "error": record,
                "provider_payload": getattr(exc, "payload", None),
            },
        )
        self._append_node_output(context, node, failure_path)
        if node.status == NodeExecutionStatus.RUNNING:
            self._finish_node(
                context,
                node,
                NodeExecutionStatus.FAILED,
                error=record,
            )
        context.task_ids.pop("seedance", None)
        context.request_ids.pop("seedance", None)

    def _persist_assets(self, context: JobContext, state: dict[str, Any]) -> None:
        assets = state.get("assets")
        if not isinstance(assets, list) or any(
            not isinstance(asset, UploadedAsset) for asset in assets
        ):
            raise ValidationError("checkpoint contains invalid uploaded assets")
        path = context.job_dir / "json/assets.json"
        write_json(path, [asset.model_dump(mode="json") for asset in assets])
        self._set_artifact(context, "assets", path)

    def _refresh_asset(self, context: JobContext, asset: UploadedAsset) -> UploadedAsset:
        if self._asset_is_fresh(asset):
            return asset
        return self.uguu_client.upload(
            asset.local_path,
            kind=asset.kind,
            raw_dir=self._raw_dir(context),
        )

    def _asset_is_fresh(self, asset: UploadedAsset) -> bool:
        try:
            uploaded_at = datetime.fromisoformat(asset.uploaded_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        if uploaded_at.tzinfo is None:
            uploaded_at = uploaded_at.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - uploaded_at).total_seconds()
        return 0 <= age_seconds < self.config.uguu_expire_hours * 3600

    def _build_cache_key(self, source_video: Path, spec: JobSpec) -> dict[str, str]:
        digest = hashlib.sha256()
        with source_video.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return {
            "source_video_hash": digest.hexdigest(),
            "target_language": spec.target_language,
            "target_region": spec.target_region,
            "target_locale": spec.target_locale,
            "doubao_model": self.config.doubao_model,
            "seedance_model": self.config.seedance_model_id,
            "analysis_prompt_version": ANALYSIS_PROMPT_VERSION,
            "seedance_prompt_version": SEEDANCE_PROMPT_VERSION,
        }
