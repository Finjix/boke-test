"""Resumable Doubao-analysis and MiniMax H3 video-localization workflow.

Doubao analyzes the complete source video once and produces the localization
package, storyboard keyframes and H3 instruction.  Seedream creates one
localized reference image per storyboard shot, then H3 transforms a short
source video or the ordered 4--15 second slices of a long source.  Every
analysis call, image attempt, H3 slice, provider response and paid attempt is
written to the job checkpoint before and after provider calls, so a restart
never silently creates a duplicate task.
"""

from __future__ import annotations

import hashlib
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from api.ark import ArkClient, extract_image_url
from api.minimax import MiniMaxClient, task_video_url
from api.uguu import UguuClient
from config import AppConfig, FIXED_SEEDREAM_MODEL, FIXED_SEEDREAM_SIZE
from core.h3_prompt import H3_PROMPT_VERSION, build_h3_content, build_transformation_prompt
from core.localization import (
    ANALYSIS_PROMPT_VERSION,
    analyze_video,
    recover_doubao_schema_wrapper,
    validate_localization_package,
)
from core.models import (
    ApprovalStatus,
    ExecutionMode,
    H3Attempt,
    H3Segment,
    JobContext,
    JobSpec,
    LocalizationPackage,
    LocalizationReferenceShot,
    NodeExecution,
    NodeExecutionStatus,
    PipelineEvent,
    PipelineResult,
    PipelineStage,
    ProviderCall,
    SeedreamAttempt,
    SeedreamReference,
    UploadedAsset,
)
from core.preflight import require_preflight, run_preflight
from media import ffprobe
from media.downloader import download
from media.ffmpeg import concat_videos, extract_frame_at, normalize_video
from media.images import inspect_image
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
from utils.history import HistoryStore
from utils.ids import new_job_id
from utils.logger import JobLogger


PIPELINE_VERSION = 7
H3_MIN_DURATION_SECONDS = 4
H3_MAX_DURATION_SECONDS = 15
H3_MAX_REFERENCE_VIDEO_SECONDS = 15.0
H3_ORIGINAL_FRAME_COUNT = 4
H3_OUTPUT_DURATION_TOLERANCE = 0.4
H3_PROVIDER = "minimax_h3"
H3_NODE = "h3"
DOUBAO_PROVIDER = "doubao"
DOUBAO_NODE = "doubao"
SEEDREAM_PROVIDER = "seedream"
SEEDREAM_NODE = "seedream"
H3_ACTIVE_STATUSES = {"queued", "running", "processing"}
H3_TERMINAL_ERROR_CODES = {
    "FAILED",
    "CANCELLED",
    "EXPIRED",
    "UNKNOWN_STATUS",
    "VIDEO_URL_MISSING",
    "EMPTY_VIDEO_RESULT",
    "TASK_ID_MISSING",
    "CREATE_OUTCOME_UNKNOWN",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _round_duration(value: float) -> int:
    """Round half up because H3 accepts integer durations only."""

    return max(H3_MIN_DURATION_SECONDS, min(H3_MAX_DURATION_SECONDS, int(value + 0.5)))


class H3VideoLocalizationPipeline:
    """Stateful local orchestration around MiniMax H3."""

    def __init__(
        self,
        config: AppConfig,
        *,
        minimax_client: Any | None = None,
        h3_client: Any | None = None,
        ark_client: Any | None = None,
        uguu_client: Any | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        cancel_event: threading.Event | None = None,
        history_store: HistoryStore | None = None,
        ffmpeg_bin: str | None = None,
    ) -> None:
        self.config = config
        self.minimax_client = minimax_client or h3_client
        self.ark_client = ark_client
        self.uguu_client = uguu_client
        self.event_callback = event_callback
        self.cancel_event = cancel_event or threading.Event()
        self.history_store = history_store or HistoryStore(config.work_dir)
        self.ffmpeg_bin = ffmpeg_bin
        self._logger: JobLogger | None = None

    # ---------- public operations ----------

    def run(
        self,
        spec: JobSpec,
        *,
        job_id: str | None = None,
        skip_preflight: bool = False,
        execution_mode: ExecutionMode | str = ExecutionMode.MANUAL,
        resume_from: Any | None = None,
    ) -> PipelineResult:
        """Create a job, analyze it with Doubao, then enter the H3 workflow."""

        del resume_from  # H3 recovery uses explicit continue/retry operations.
        try:
            mode = ExecutionMode(execution_mode)
        except ValueError as exc:
            raise ValidationError("execution_mode must be 'manual' or 'auto'") from exc

        if job_id:
            checkpoint = self.config.work_dir / job_id / "checkpoint.json"
            if checkpoint.is_file():
                context, _ = self._load_existing_context(job_id)
                if (
                    context.stage in {PipelineStage.ANALYZING, PipelineStage.FAILED}
                    and not context.h3_segments
                    and not context.seedream_references
                    and self._recover_analysis_completion(context)
                ):
                    return self._pause_for_doubao_approval(context)
                if context.stage == PipelineStage.WAITING_FOR_APPROVAL:
                    return self._result(context, action_required="approve_doubao")
                if context.stage == PipelineStage.GENERATING_REFERENCES:
                    if self._recover_seedream_completion(context):
                        return self._result(context, action_required="approve_seedream")
                    return self._result(context, action_required="retry_seedream")
                if context.stage == PipelineStage.WAITING_FOR_REFERENCE_APPROVAL:
                    return self._result(context, action_required="approve_seedream")
                return self._result(context)

        context = self._prepare_new_context(spec, job_id=job_id, execution_mode=mode)
        self._initialize_runtime(context)
        try:
            self._check_cancel()
            master = self._source_master_path(context)
            info = ffprobe.probe(
                master,
                ffprobe_bin=self.config.ffprobe_bin,
                timeout=self.config.http_timeout,
            )
            if not info.has_video:
                raise ValidationError("input video must contain a video stream")
            self._record_master_info(context, info.raw, info.duration)

            if not skip_preflight:
                self._run_and_require_preflight(context, spec)
            self._run_doubao_analysis(context, info.duration)
            if context.execution_mode == ExecutionMode.MANUAL:
                return self._pause_for_doubao_approval(context)
            return self._continue_after_doubao(context)
        except PipelineCancelled as exc:
            self._handle_outer_error(context, exc)
            raise
        except Exception as exc:  # noqa: BLE001 - checkpoint every failure
            self._handle_outer_error(context, exc)
            raise self._normalize_exception(exc) from exc

    def approve_doubao(self, job_id: str) -> PipelineResult:
        """Approve the saved Doubao plan and generate storyboard references."""

        context, _ = self._load_existing_context(job_id)
        if context.stage == PipelineStage.ANALYZING:
            if not self._recover_analysis_completion(context):
                raise ValidationError("Doubao result is not ready for approval")
            self._pause_for_doubao_approval(context)
        elif (
            context.stage == PipelineStage.FAILED
            and not context.h3_segments
            and not context.seedream_references
        ):
            if not self._recover_analysis_completion(context):
                if self._recover_saved_doubao_package(context) is None:
                    raise ValidationError("Doubao result is not ready for approval")
            self._pause_for_doubao_approval(context)
        if context.stage != PipelineStage.WAITING_FOR_APPROVAL:
            raise ValidationError("任务当前不在等待 Doubao 确认状态")
        if context.approval_status != ApprovalStatus.PENDING:
            raise ValidationError("任务没有待确认的 Doubao 结果")
        # Validate both durable artifacts before changing approval state.  A
        # partially-written or mismatched package must remain explicitly
        # retryable instead of leaving an APPROVED checkpoint that cannot
        # enter H3.
        self._load_analysis_package(context)
        context.approval_status = ApprovalStatus.APPROVED
        context.approved_at = _now_iso()
        context.last_error = None
        self._save_checkpoint(context)
        return self._continue_after_doubao(context)

    approve_analysis = approve_doubao

    def approve_seedream(self, job_id: str) -> PipelineResult:
        """Approve all generated storyboard references and enter H3."""

        context, _ = self._load_existing_context(job_id)
        if context.stage == PipelineStage.GENERATING_REFERENCES:
            self._recover_seedream_completion(context)
        if context.stage != PipelineStage.WAITING_FOR_REFERENCE_APPROVAL:
            raise ValidationError("任务当前不在等待 Seedream 参考图确认状态")
        if context.approval_status != ApprovalStatus.PENDING:
            raise ValidationError("任务没有待确认的 Seedream 参考图结果")
        self._validate_seedream_references(context)
        context.approval_status = ApprovalStatus.APPROVED
        context.approved_at = _now_iso()
        context.pending_approval = None
        context.last_error = None
        self._save_checkpoint(context)
        return self._continue_after_reference_generation(context)

    def retry_seedream(self, job_id: str, shot_id: str | None = None) -> PipelineResult:
        """Explicitly retry one failed/stale Seedream storyboard image."""

        context, _ = self._load_existing_context(job_id)
        if context.h3_segments:
            raise ValidationError("H3 已开始后不能重试 Seedream；请重试对应 H3 片段")
        if context.stage not in {
            PipelineStage.FAILED,
            PipelineStage.GENERATING_REFERENCES,
            PipelineStage.WAITING_FOR_REFERENCE_APPROVAL,
        }:
            raise ValidationError("任务当前不允许重试 Seedream")
        if not context.seedream_references:
            raise ValidationError("Seedream 参考图计划尚未生成")
        for reference in context.seedream_references:
            if reference.status == "completed" and not self._seedream_output_is_valid(
                context, reference
            ):
                reference.status = "stale"
                reference.stale_reason = "本地参考图缺失或校验失败"
                self._invalidate_following_seedream_references(
                    context,
                    reference.shot_id,
                )
        self._save_checkpoint(context)
        target = self._select_seedream_reference(context, shot_id)
        if target.status == "completed":
            raise ValidationError(
                f"Seedream 参考图 {target.shot_id} 已完成且有效，无需重复生成"
            )
        self._invalidate_following_seedream_references(context, target.shot_id)
        self._write_seedream_reference_manifest(context)
        self._save_checkpoint(context)
        context.last_error = None
        try:
            self._run_seedream_reference(context, target)
            self._run_pending_seedream_references(context)
            if all(item.status == "completed" for item in context.seedream_references):
                return self._pause_for_seedream_approval(context)
            raise ValidationError("仍有 Seedream 参考图未完成")
        except PipelineCancelled as exc:
            self._handle_outer_error(context, exc)
            raise
        except Exception as exc:  # noqa: BLE001 - retain explicit retry evidence
            self._handle_seedream_outer_error(context, exc)
            raise self._normalize_exception(exc) from exc

    retry_reference = retry_seedream

    def retry_doubao(self, job_id: str) -> PipelineResult:
        """Create a fresh Doubao analysis attempt without touching H3 attempts."""

        context, _ = self._load_existing_context(job_id)
        if context.stage in {
            PipelineStage.ANALYZING,
            PipelineStage.FAILED,
            PipelineStage.WAITING_FOR_APPROVAL,
        } and not context.h3_segments and not context.seedream_references:
            try:
                if self._recover_analysis_completion(context):
                    return self._pause_for_doubao_approval(context)
            except ValidationError:
                # A damaged/incomplete package is not a recoverable result,
                # but it must still be handled by the explicit retry action.
                pass
        latest = self._latest_node(context, DOUBAO_NODE)
        if context.stage == PipelineStage.ANALYZING and (
            latest is None or latest.status != NodeExecutionStatus.FAILED
        ):
            interrupted = ErrorRecord(
                stage=PipelineStage.ANALYZING.value,
                message="Doubao 分析进程中断，未找到有效的 package",
                provider=DOUBAO_PROVIDER,
                error_code="INTERRUPTED",
                retryable=False,
            ).as_dict()
            if latest is None:
                failure_path = self._doubao_attempt_dir(context, 1) / "failure.json"
                write_json(failure_path, {"error": interrupted})
                latest = NodeExecution(
                    node=DOUBAO_NODE,
                    attempt=1,
                    status=NodeExecutionStatus.FAILED,
                    provider=DOUBAO_PROVIDER,
                    started_at=_now_iso(),
                    finished_at=_now_iso(),
                    error=interrupted,
                    output_artifacts=[self._path_reference(context, failure_path)],
                )
                context.node_executions.append(latest)
            else:
                failure_path = self._doubao_attempt_dir(context, latest.attempt) / "failure.json"
                write_json(failure_path, {"error": interrupted})
                self._finish_node(
                    context,
                    latest,
                    NodeExecutionStatus.FAILED,
                    error=interrupted,
                    output_artifacts=[self._path_reference(context, failure_path)],
                )
            context.stage = PipelineStage.FAILED
            context.last_error = interrupted
            self._save_checkpoint(context)
        elif context.stage == PipelineStage.ANALYZING and latest is not None:
            context.stage = PipelineStage.FAILED
            self._save_checkpoint(context)
        if (
            context.stage != PipelineStage.FAILED
            or latest is None
            or latest.status != NodeExecutionStatus.FAILED
        ):
            raise ValidationError("只有失败的 Doubao 分析节点可以重试")
        context.last_error = None
        context.approval_status = ApprovalStatus.NOT_REQUIRED
        try:
            duration = self._source_master_duration(context)
            self._run_doubao_analysis(context, duration)
            if context.execution_mode == ExecutionMode.MANUAL:
                return self._pause_for_doubao_approval(context)
            return self._continue_after_doubao(context)
        except PipelineCancelled as exc:
            self._handle_outer_error(context, exc)
            raise
        except Exception as exc:  # noqa: BLE001 - preserve the failed analysis node
            self._handle_outer_error(context, exc)
            raise self._normalize_exception(exc) from exc

    retry_analysis = retry_doubao

    def _continue_after_analysis(self, context: JobContext) -> PipelineResult:
        """Compatibility alias for the old analysis continuation name."""

        return self._continue_after_doubao(context)

    def _continue_after_doubao(self, context: JobContext) -> PipelineResult:
        """Generate Seedream references after the Doubao plan is approved."""

        self._load_analysis_package(context)
        self._run_seedream_references(context)
        if context.execution_mode == ExecutionMode.MANUAL:
            return self._pause_for_seedream_approval(context)
        return self._continue_after_reference_generation(context)

    def _continue_after_reference_generation(self, context: JobContext) -> PipelineResult:
        """Enter H3 only after every storyboard reference is valid."""

        self._load_analysis_package(context)
        self._validate_seedream_references(context)
        context.pending_approval = None
        duration = self._source_master_duration(context)
        if duration > H3_MAX_DURATION_SECONDS:
            self._set_stage(context, PipelineStage.WAITING_FOR_SEGMENTS)
            self._emit(
                context,
                "segments_required",
                "Doubao 分析已完成；源视频超过 15 秒，请按顺序上传 4–15 秒片段",
                source_duration_seconds=round(duration, 3),
                next_segment_index=1,
            )
            self._save_checkpoint(context)
            return self._result(
                context,
                action_required="append_segment",
                next_segment_index=1,
                message="请按顺序上传每片 4–15 秒的视频",
            )
        master = self._source_master_path(context)
        segment = self._add_segment_from_path(context, master, index=1)
        return self._run_segment(context, segment)

    def _pause_for_doubao_approval(self, context: JobContext) -> PipelineResult:
        """Persist the Doubao result and wait before paying for Seedream."""

        self._load_analysis_package(context)
        context.approval_status = ApprovalStatus.PENDING
        context.pending_approval = DOUBAO_NODE
        self._set_stage(context, PipelineStage.WAITING_FOR_APPROVAL)
        package_path = self._artifact_path(context, "localization_package", required=False)
        prompt_path = self._artifact_path(context, "doubao_h3_prompt", required=False)
        self._emit(
            context,
            "approval_required",
            "Doubao 分析已完成，请检查分镜方案后确认生成 Seedream 参考图",
            package_path=str(package_path) if package_path else None,
            prompt_path=str(prompt_path) if prompt_path else None,
        )
        self._save_checkpoint(context)
        return self._result(context, action_required="approve_doubao")

    _pause_for_approval = _pause_for_doubao_approval

    def _pause_for_seedream_approval(self, context: JobContext) -> PipelineResult:
        """Persist all Seedream references and wait before creating H3."""

        self._validate_seedream_references(context)
        context.approval_status = ApprovalStatus.PENDING
        context.pending_approval = SEEDREAM_NODE
        self._set_stage(context, PipelineStage.WAITING_FOR_REFERENCE_APPROVAL)
        manifest_path = self._artifact_path(context, "seedream_reference_manifest", required=False)
        self._emit(
            context,
            "reference_approval_required",
            "Seedream 参考图已生成，请检查后确认进入 H3",
            manifest_path=str(manifest_path) if manifest_path else None,
        )
        self._save_checkpoint(context)
        return self._result(context, action_required="approve_seedream")

    def _run_doubao_analysis(self, context: JobContext, duration: float) -> LocalizationPackage:
        """Analyze the copied source master exactly once per explicit attempt."""

        self._ensure_clients()
        self._set_stage(context, PipelineStage.ANALYZING)
        source = self._source_master_path(context)
        references_path = self._artifact_path(context, "references", required=False)
        input_artifacts = [self._path_reference(context, source)]
        if references_path is not None:
            input_artifacts.append(self._path_reference(context, references_path))
        node = self._begin_node(
            context,
            DOUBAO_NODE,
            provider=DOUBAO_PROVIDER,
            input_artifacts=input_artifacts,
        )
        attempt_dir = self._doubao_attempt_dir(context, node.attempt)
        raw_dir = attempt_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        try:
            source_asset = self._ensure_asset(
                context,
                None,
                source,
                kind="doubao_source_video",
            )
            source_asset_path = context.job_dir / "json" / "doubao_source_asset.json"
            write_json(source_asset_path, source_asset.model_dump(mode="json"))
            self._set_artifact(context, "doubao_source_asset", source_asset_path)
            package = analyze_video(
                self.ark_client,
                source_asset.remote_url,
                target_language=context.spec.target_language,
                target_region=context.spec.target_region,
                target_locale=context.spec.target_locale,
                duration_seconds=duration,
                raw_dir=raw_dir,
                logger=self._logger,
                transformation_instruction=context.spec.transformation_instruction,
                require_h3_prompt=True,
                require_reference_plan=True,
                attempt_callback=lambda detail: self._record_provider_call(
                    context,
                    node,
                    detail,
                ),
            )
            package_path = context.job_dir / "json" / "localization_package.json"
            prompt_path = context.job_dir / "json" / "doubao_h3_prompt.txt"
            attempt_package_path = attempt_dir / "package.json"
            attempt_prompt_path = attempt_dir / "h3_prompt.txt"
            package_data = package.model_dump(mode="json")
            write_json(attempt_package_path, package_data)
            attempt_prompt_path.write_text(f"{package.h3_prompt}\n", encoding="utf-8")
            write_json(package_path, package_data)
            prompt_path.write_text(f"{package.h3_prompt}\n", encoding="utf-8")
            self._set_artifact(context, "localization_package", package_path)
            self._set_artifact(context, "doubao_h3_prompt", prompt_path)
            context.metrics["speaker_count"] = len(package.speakers)
            context.metrics["dialogue_count"] = len(package.dialogues)
            context.metrics["analysis_prompt_version"] = ANALYSIS_PROMPT_VERSION
            self._finish_node(
                context,
                node,
                NodeExecutionStatus.COMPLETED,
                output_artifacts=[
                    self._path_reference(context, source_asset_path),
                    self._path_reference(context, attempt_package_path),
                    self._path_reference(context, attempt_prompt_path),
                    self._path_reference(context, package_path),
                    self._path_reference(context, prompt_path),
                ],
            )
            if self._logger:
                self._logger.info(
                    "Doubao localization analysis completed",
                    job_id=context.job_id,
                    target_locale=context.spec.target_locale,
                    speaker_count=len(package.speakers),
                    dialogue_count=len(package.dialogues),
                )
            return package
        except Exception as exc:  # noqa: BLE001 - retain the analysis attempt evidence
            record = self._error_record(PipelineStage.ANALYZING, exc).as_dict()
            failure_path = attempt_dir / "failure.json"
            write_json(
                failure_path,
                {
                    "error": record,
                    "provider_payload": getattr(exc, "payload", None),
                },
            )
            self._finish_node(
                context,
                node,
                NodeExecutionStatus.FAILED,
                error=record,
                output_artifacts=[self._path_reference(context, failure_path)],
            )
            raise

    # ---------- Seedream storyboard references ----------

    def _run_seedream_references(self, context: JobContext) -> None:
        """Create one low-cost target reference image for every Doubao shot."""

        self._ensure_clients()
        self._set_stage(context, PipelineStage.GENERATING_REFERENCES)
        try:
            package = self._load_analysis_package(context)
            if not context.seedream_references:
                context.seedream_references = self._prepare_seedream_references(context, package)
                self._write_seedream_reference_manifest(context)
                self._save_checkpoint(context)
            self._run_pending_seedream_references(context)
            self._validate_seedream_references(context)
        except Exception:
            # The failed Seedream node and its durable evidence are written by
            # _run_seedream_reference.  Keep the job explicitly retryable.
            context.stage = PipelineStage.FAILED
            self._save_checkpoint(context)
            raise
        self._set_stage(context, PipelineStage.WAITING_FOR_REFERENCE_APPROVAL)

    def _prepare_seedream_references(
        self,
        context: JobContext,
        package: LocalizationPackage,
    ) -> list[SeedreamReference]:
        if not package.reference_shots:
            raise ValidationError("Doubao package has no storyboard reference shots")
        source = self._source_master_path(context)
        frame_dir = context.job_dir / "input" / "storyboard"
        references: list[SeedreamReference] = []
        for shot in package.reference_shots:
            frame_path = frame_dir / f"{shot.shot_id}_{shot.keyframe_ms:010d}.png"
            if not frame_path.is_file():
                extract_frame_at(
                    source,
                    frame_path,
                    timestamp_seconds=shot.keyframe_ms / 1000.0,
                    ffprobe_bin=self.config.ffprobe_bin,
                    ffmpeg_bin=self.ffmpeg_bin,
                    timeout=max(self.config.http_timeout, 300.0),
                )
            inspect_image(frame_path)
            references.append(
                SeedreamReference(
                    shot_id=shot.shot_id,
                    start_ms=shot.start_ms,
                    end_ms=shot.end_ms,
                    keyframe_ms=shot.keyframe_ms,
                    continuity_group=shot.continuity_group,
                    prompt=self._build_seedream_prompt(context, shot),
                    source_frame_artifact=self._path_reference(context, frame_path),
                )
            )
        return references

    @staticmethod
    def _build_seedream_prompt(
        context: JobContext,
        shot: LocalizationReferenceShot,
    ) -> str:
        replacements = "; ".join(shot.replacement_requirements) or "all culturally specific visible details"
        preserves = "; ".join(shot.preserve_requirements) or (
            "character count, character relationships, pose, action beat, camera composition, "
            "object placement, depth and lighting direction"
        )
        characters = ", ".join(shot.character_ids) if shot.character_ids else "all visible people"
        return "\n".join(
            [
                "Edit the supplied source keyframe into one target-region storyboard reference image.",
                f"Target region: {context.spec.target_region}; target locale: {context.spec.target_locale}.",
                f"Rebuild the appearance and wardrobe of {characters} for the target region, and replace {replacements}.",
                f"Scene direction: {shot.scene_description}.",
                f"Preserve exactly: {preserves}.",
                f"Additional Seedream direction: {shot.seedream_prompt}",
                "This is an image-edit operation, not a generic style transfer. The output must "
                "contain the localized people and localized background together, with no source "
                "facade, source-language signage, unintended subtitles, watermark or unrelated subject.",
                "If a previous Seedream storyboard reference image is supplied as a second input, "
                "use it as the continuity anchor for identity, wardrobe, scene design, architecture, "
                "signage style, vehicles, props, color and lighting. Use the current source keyframe "
                "as the authority for this shot's pose, framing, camera geography and object timing; "
                "do not copy the previous image's pose or composition.",
            ]
        )

    def _run_pending_seedream_references(self, context: JobContext) -> None:
        for reference in context.seedream_references:
            self._check_cancel()
            if reference.status == "completed" and self._seedream_output_is_valid(context, reference):
                continue
            if reference.status == "completed":
                reference.status = "stale"
                reference.stale_reason = "本地参考图缺失或校验失败"
                self._invalidate_following_seedream_references(
                    context,
                    reference.shot_id,
                )
                self._write_seedream_reference_manifest(context)
                self._save_checkpoint(context)
            if reference.status == "running":
                self._mark_seedream_interrupted(context, reference)
            self._run_seedream_reference(context, reference)

    def _invalidate_following_seedream_references(
        self,
        context: JobContext,
        shot_id: str,
    ) -> None:
        """Invalidate later images when a continuity-chain predecessor changes."""

        try:
            index = next(
                index
                for index, reference in enumerate(context.seedream_references)
                if reference.shot_id == shot_id
            )
        except StopIteration as exc:
            raise ValidationError(
                f"Seedream reference is not present in the storyboard: {shot_id}"
            ) from exc

        for following in context.seedream_references[index + 1 :]:
            if following.status == "completed":
                following.status = "stale"
                following.stale_reason = (
                    "前序 Seedream 参考图已重新生成，需要按连续性链重生成"
                )

    def _previous_seedream_input(
        self,
        context: JobContext,
        reference: SeedreamReference,
    ) -> tuple[SeedreamReference, Path, UploadedAsset] | None:
        """Return the immediately previous generated image as a continuity input."""

        try:
            index = next(
                index
                for index, item in enumerate(context.seedream_references)
                if item.shot_id == reference.shot_id
            )
        except StopIteration as exc:
            raise ValidationError(
                f"Seedream reference is not present in the storyboard: {reference.shot_id}"
            ) from exc
        if index == 0:
            return None

        previous = context.seedream_references[index - 1]
        if previous.status != "completed":
            raise ValidationError(
                f"上一张 Seedream 参考图未完成，不能生成当前镜头: {reference.shot_id}"
            )
        previous_path = self._absolute_path(context, previous.output_artifact)
        if previous_path is None or not previous_path.is_file():
            raise ValidationError(
                f"上一张 Seedream 参考图输出缺失: {previous.shot_id}"
            )
        inspect_image(previous_path)
        previous_asset = self._ensure_asset(
            context,
            previous.reference_asset,
            previous_path,
            kind=f"seedream_shot_{previous.shot_id}_continuity_reference",
        )
        if previous.reference_asset != previous_asset:
            previous.reference_asset = previous_asset
            self._write_seedream_reference_manifest(context)
            self._save_checkpoint(context)
        return previous, previous_path, previous_asset

    def _run_seedream_reference(
        self,
        context: JobContext,
        reference: SeedreamReference,
    ) -> None:
        self._ensure_clients()
        source_path = self._absolute_path(context, reference.source_frame_artifact)
        if source_path is None or not source_path.is_file():
            raise ValidationError(f"Seedream source frame is missing: {reference.shot_id}")
        inspect_image(source_path)
        attempt_number = max((item.attempt for item in reference.attempts), default=0) + 1
        attempt = SeedreamAttempt(
            attempt=attempt_number,
            status=NodeExecutionStatus.RUNNING,
            started_at=_now_iso(),
            source_frame_artifact=self._path_reference(context, source_path),
            source_frame_asset=reference.source_frame_asset,
        )
        reference.attempts.append(attempt)
        reference.active_attempt = attempt_number
        reference.status = "running"
        reference.stale_reason = None
        node = NodeExecution(
            node=SEEDREAM_NODE,
            attempt=attempt_number,
            status=NodeExecutionStatus.RUNNING,
            provider=SEEDREAM_PROVIDER,
            shot_id=reference.shot_id,
            started_at=attempt.started_at,
            input_artifacts=[self._path_reference(context, source_path)],
        )
        context.node_executions.append(node)
        self._save_checkpoint(context)
        self._emit(
            context,
            "node_started",
            f"Seedream shot {reference.shot_id} attempt {attempt_number} started",
            node=SEEDREAM_NODE,
            shot_id=reference.shot_id,
            attempt=attempt_number,
        )
        attempt_dir = self._seedream_attempt_dir(context, reference, attempt)
        raw_dir = attempt_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        provider_call_recorded = False
        response: Any = None
        try:
            source_asset = self._ensure_asset(
                context,
                reference.source_frame_asset,
                source_path,
                kind=f"seedream_shot_{reference.shot_id}_source_frame",
            )
            reference.source_frame_asset = source_asset
            attempt.source_frame_asset = source_asset
            image_urls = [source_asset.remote_url]
            previous_input = self._previous_seedream_input(context, reference)
            if previous_input is not None:
                previous_reference, previous_path, previous_asset = previous_input
                image_urls.append(previous_asset.remote_url)
                attempt.continuity_reference_shot_id = previous_reference.shot_id
                attempt.continuity_reference_artifact = self._path_reference(
                    context,
                    previous_path,
                )
                node.input_artifacts.append(attempt.continuity_reference_artifact)
            self._save_checkpoint(context)
            request_artifact = attempt_dir / "request.json"
            request = {
                "model": FIXED_SEEDREAM_MODEL,
                "prompt": reference.prompt,
                "image": image_urls,
                "size": FIXED_SEEDREAM_SIZE,
                "stream": False,
                "response_format": "url",
                "watermark": False,
            }
            write_json(request_artifact, request)
            attempt.request_artifact = self._path_reference(context, request_artifact)
            node.output_artifacts.append(attempt.request_artifact)
            self._save_checkpoint(context)
            response = self.ark_client.generate_image(
                image_urls,
                reference.prompt,
                stage=f"seedream_{reference.shot_id}_attempt_{attempt_number}",
                raw_dir=raw_dir,
                size=FIXED_SEEDREAM_SIZE,
                watermark=False,
            )
            request_id = getattr(response, "request_id", None) or getattr(
                self.ark_client, "last_request_id", None
            )
            attempt.request_id = str(request_id) if request_id else None
            if request_id and str(request_id) not in node.request_ids:
                node.request_ids.append(str(request_id))
            response_path = getattr(response, "raw_path", None)
            if response_path:
                attempt.raw_response_artifact = self._path_reference(context, Path(response_path))
                node.output_artifacts.append(attempt.raw_response_artifact)
            response_artifact = attempt_dir / "response.json"
            data = response.data if hasattr(response, "data") else response
            write_json(response_artifact, data)
            attempt.response_artifact = self._path_reference(context, response_artifact)
            if attempt.response_artifact not in node.output_artifacts:
                node.output_artifacts.append(attempt.response_artifact)
            self._record_provider_call(
                context,
                node,
                {
                    "status": "completed",
                    "request_id": request_id,
                    "raw_response_path": attempt.raw_response_artifact or attempt.response_artifact,
                    "started_at": attempt.started_at,
                    "finished_at": _now_iso(),
                },
            )
            provider_call_recorded = True
            image_url = extract_image_url(response)
            provider_output = attempt_dir / "provider_output.png"
            download(
                image_url,
                provider_output,
                timeout=self.config.http_timeout,
                attempts=self.config.max_retries,
            )
            image_info = inspect_image(provider_output)
            output_path = attempt_dir / "reference.png"
            shutil.copy2(provider_output, output_path)
            info_path = attempt_dir / "reference_info.json"
            write_json(
                info_path,
                {
                    "width": image_info.width,
                    "height": image_info.height,
                    "format": image_info.format,
                    "source_frame": self._path_reference(context, source_path),
                },
            )
            output_asset = self._ensure_asset(
                context,
                None,
                output_path,
                kind=f"seedream_shot_{reference.shot_id}_reference",
            )
            attempt.provider_output_artifact = self._path_reference(context, provider_output)
            attempt.output_artifact = self._path_reference(context, output_path)
            reference.reference_asset = output_asset
            reference.output_artifact = attempt.output_artifact
            attempt.finished_at = _now_iso()
            attempt.status = NodeExecutionStatus.COMPLETED
            reference.active_attempt = None
            reference.status = "completed"
            for artifact in (
                attempt.provider_output_artifact,
                attempt.output_artifact,
                self._path_reference(context, info_path),
            ):
                if artifact not in node.output_artifacts:
                    node.output_artifacts.append(artifact)
            self._write_seedream_reference_manifest(context)
            self._save_checkpoint(context)
            self._finish_node(
                context,
                node,
                NodeExecutionStatus.COMPLETED,
                output_artifacts=[attempt.output_artifact],
            )
            self._emit(
                context,
                "reference_completed",
                f"Seedream shot {reference.shot_id} completed",
                shot_id=reference.shot_id,
                attempt=attempt.attempt,
                output=attempt.output_artifact,
            )
        except Exception as exc:  # noqa: BLE001 - preserve paid image evidence
            if isinstance(exc, ProviderError):
                request_id = exc.request_id or getattr(self.ark_client, "last_request_id", None)
                if request_id:
                    attempt.request_id = str(request_id)
                    if str(request_id) not in node.request_ids:
                        node.request_ids.append(str(request_id))
                if exc.raw_response_path and not attempt.raw_response_artifact:
                    attempt.raw_response_artifact = self._path_reference(
                        context, Path(exc.raw_response_path)
                    )
            if not provider_call_recorded:
                self._record_provider_call(
                    context,
                    node,
                    {
                        "status": "failed",
                        "request_id": attempt.request_id,
                        "raw_response_path": attempt.raw_response_artifact or attempt.response_artifact,
                        "started_at": attempt.started_at,
                        "finished_at": _now_iso(),
                        "error": {
                            "message": str(exc),
                            "error_code": getattr(exc, "error_code", None),
                        },
                    },
                )
            record = self._error_record(PipelineStage.GENERATING_REFERENCES, exc).as_dict()
            failure_path = attempt_dir / "failure.json"
            write_json(
                failure_path,
                {
                    "error": record,
                    "provider_payload": getattr(exc, "payload", None),
                },
            )
            attempt.failure_artifact = self._path_reference(context, failure_path)
            attempt.error = record
            attempt.status = NodeExecutionStatus.FAILED
            attempt.finished_at = _now_iso()
            reference.active_attempt = None
            reference.status = "failed"
            node.status = NodeExecutionStatus.FAILED
            node.finished_at = attempt.finished_at
            node.error = record
            if attempt.failure_artifact not in node.output_artifacts:
                node.output_artifacts.append(attempt.failure_artifact)
            context.stage = PipelineStage.FAILED
            context.progress = 0
            context.last_error = record
            self._write_seedream_reference_manifest(context)
            self._save_checkpoint(context)
            self._emit(
                context,
                "node_failed",
                record["message"],
                node=SEEDREAM_NODE,
                shot_id=reference.shot_id,
                attempt=attempt.attempt,
                error=record,
            )
            raise

    def _recover_seedream_completion(self, context: JobContext) -> bool:
        """Promote locally complete synchronous image calls without new calls."""

        if not context.seedream_references:
            return False
        for reference in context.seedream_references:
            if reference.status == "running":
                self._mark_seedream_interrupted(context, reference)
            if reference.status == "completed" and not self._seedream_output_is_valid(
                context, reference
            ):
                reference.status = "stale"
                reference.stale_reason = "本地参考图缺失或校验失败"
            if reference.status == "completed" and self._seedream_output_is_valid(
                context, reference
            ):
                self._promote_seedream_node_completion(context, reference)
            if reference.status != "completed" or not self._seedream_output_is_valid(
                context, reference
            ):
                self._write_seedream_reference_manifest(context)
                self._save_checkpoint(context)
                return False
        context.approval_status = ApprovalStatus.PENDING
        context.pending_approval = SEEDREAM_NODE
        self._set_stage(context, PipelineStage.WAITING_FOR_REFERENCE_APPROVAL)
        return True

    @staticmethod
    def _promote_seedream_node_completion(
        context: JobContext,
        reference: SeedreamReference,
    ) -> None:
        """Close the crash window between an image and its node checkpoint."""

        completed_attempt = next(
            (
                attempt
                for attempt in reversed(reference.attempts)
                if attempt.status == NodeExecutionStatus.COMPLETED
                and attempt.output_artifact
            ),
            None,
        )
        if completed_attempt is None:
            return
        node = H3VideoLocalizationPipeline._seedream_node_for_attempt(
            context,
            reference.shot_id,
            completed_attempt.attempt,
        )
        if node is None or node.status != NodeExecutionStatus.RUNNING:
            return
        node.status = NodeExecutionStatus.COMPLETED
        node.finished_at = completed_attempt.finished_at or _now_iso()
        if completed_attempt.output_artifact not in node.output_artifacts:
            node.output_artifacts.append(completed_attempt.output_artifact)

    def _seedream_output_is_valid(
        self,
        context: JobContext,
        reference: SeedreamReference,
    ) -> bool:
        path = self._absolute_path(context, reference.output_artifact)
        if path is None or not path.is_file():
            return False
        try:
            inspect_image(path)
        except ValidationError:
            return False
        return True

    def _validate_seedream_references(self, context: JobContext) -> None:
        if not context.seedream_references:
            raise ValidationError("没有可用的 Seedream 参考图")
        incomplete = [
            item.shot_id
            for item in context.seedream_references
            if item.status != "completed" or not self._seedream_output_is_valid(context, item)
        ]
        if incomplete:
            raise ValidationError(f"Seedream 参考图尚未全部完成：{incomplete[0]}")

    @staticmethod
    def _select_seedream_reference(
        context: JobContext,
        shot_id: str | None,
    ) -> SeedreamReference:
        if shot_id:
            reference = next(
                (item for item in context.seedream_references if item.shot_id == shot_id),
                None,
            )
            if reference is None:
                raise ValidationError(f"Seedream shot {shot_id} was not found")
            return reference
        reference = next(
            (
                item
                for item in context.seedream_references
                if item.status != "completed"
            ),
            None,
        )
        if reference is None:
            raise ValidationError("没有失败或待处理的 Seedream 参考图")
        return reference

    def _mark_seedream_interrupted(
        self,
        context: JobContext,
        reference: SeedreamReference,
    ) -> None:
        attempt = next(
            (
                item
                for item in reference.attempts
                if item.attempt == reference.active_attempt
                and item.status == NodeExecutionStatus.RUNNING
            ),
            None,
        )
        if attempt is None:
            reference.status = "failed"
            reference.active_attempt = None
            return
        record = ErrorRecord(
            stage=PipelineStage.GENERATING_REFERENCES.value,
            message="Seedream 图像生成在保存结果前中断，只能显式重试",
            provider=SEEDREAM_PROVIDER,
            error_code="CREATE_OUTCOME_UNKNOWN",
            retryable=False,
        ).as_dict()
        attempt.status = NodeExecutionStatus.FAILED
        attempt.finished_at = _now_iso()
        attempt.error = record
        reference.status = "failed"
        reference.active_attempt = None
        failure_path = self._seedream_attempt_dir(context, reference, attempt) / "failure.json"
        write_json(failure_path, {"error": record})
        attempt.failure_artifact = self._path_reference(context, failure_path)
        node = self._seedream_node_for_attempt(context, reference.shot_id, attempt.attempt)
        if node is not None:
            node.status = NodeExecutionStatus.FAILED
            node.finished_at = attempt.finished_at
            node.error = record
            node.output_artifacts.append(attempt.failure_artifact)
        context.stage = PipelineStage.FAILED
        context.last_error = record
        self._save_checkpoint(context)

    def _handle_seedream_outer_error(self, context: JobContext, exc: Exception) -> None:
        if context.stage != PipelineStage.FAILED:
            self._fail_job(context, PipelineStage.GENERATING_REFERENCES, exc)
        else:
            context.last_error = self._error_record(PipelineStage.GENERATING_REFERENCES, exc).as_dict()
            self._save_checkpoint(context)

    def _write_seedream_reference_manifest(self, context: JobContext) -> None:
        path = context.job_dir / "json" / "seedream_reference_manifest.json"
        write_json(
            path,
            {
                "model": self.config.seedream_model,
                "size": FIXED_SEEDREAM_SIZE,
                "watermark": False,
                "references": [item.model_dump(mode="json") for item in context.seedream_references],
            },
        )
        self._set_artifact(context, "seedream_reference_manifest", path)

    def append_segment(self, job_id: str, video_path: Path) -> PipelineResult:
        """Append the next ordered source slice and create its H3 task."""

        context, _ = self._load_existing_context(job_id)
        if context.stage not in {
            PipelineStage.WAITING_FOR_SEGMENTS,
            PipelineStage.WAITING_FOR_NEXT_SEGMENT,
        }:
            raise ValidationError(
                "只能在等待片段时上传下一片；请先完成或重试当前 H3 片段"
            )
        if not Path(video_path).is_file():
            raise ValidationError(f"segment video does not exist: {video_path}")
        if any(
            segment.status in {"pending", "running"}
            for segment in context.h3_segments
        ):
            raise ValidationError("已有片段尚未完成，不能跳过顺序上传")
        next_index = len(context.h3_segments) + 1
        uploaded_source = self._copy_uploaded_segment(context, Path(video_path), next_index)
        previous_stage = context.stage
        try:
            segment = self._add_segment_from_path(context, uploaded_source, index=next_index)
        except Exception as exc:
            # A local validation/ffmpeg error should not destroy the waiting job.
            context.stage = previous_stage
            context.last_error = self._error_record(previous_stage, exc).as_dict()
            self._save_checkpoint(context)
            raise self._normalize_exception(exc) from exc
        return self._run_segment(context, segment)

    add_segment = append_segment

    def continue_segment(
        self,
        job_id: str,
        segment_index: int | None = None,
    ) -> PipelineResult:
        """Resume polling an existing H3 task without creating another one."""

        context, _ = self._load_existing_context(job_id)
        segment = self._select_segment(context, segment_index)
        attempt = self._active_attempt(segment)
        if attempt is None:
            if segment.status == "completed":
                return self._result(context, segment_index=segment.index)
            raise ValidationError("没有可继续轮询的 H3 task ID，请使用 retry_segment")
        if not attempt.task_id:
            self._mark_unknown_running_attempt(
                context,
                segment,
                attempt,
                "H3 task 在保存 task ID 前中断，无法判断是否已创建；请明确重试该片段",
            )
            raise ValidationError(
                "H3 task ID 尚未保存，无法安全继续；请明确执行 retry_segment"
            )
        context.last_error = None
        context.stage = PipelineStage.GENERATING_SEGMENT
        context.progress = self._segment_progress(segment.index)
        self._save_checkpoint(context)
        try:
            return self._wait_and_finalize_segment(context, segment, attempt)
        except Exception as exc:  # noqa: BLE001 - preserve running/failed state
            self._handle_attempt_error(context, segment, attempt, exc)
            raise self._normalize_exception(exc) from exc

    continue_h3 = continue_segment

    def retry_segment(
        self,
        job_id: str,
        segment_index: int | None = None,
        *,
        refresh_prompt: bool = False,
    ) -> PipelineResult:
        """Create a fresh H3 attempt for one segment.

        Normal retries reuse the persisted prompt exactly.  An explicit
        ``refresh_prompt`` is reserved for a user-approved prompt correction;
        it may also regenerate an otherwise completed segment while retaining
        all previous attempts and outputs as evidence.
        """

        context, _ = self._load_existing_context(job_id)
        segment = self._select_segment(context, segment_index)
        if segment.status == "completed" and not refresh_prompt:
            raise ValidationError("已完成的 H3 片段不能重试")
        active = self._active_attempt(segment)
        if active is not None and active.task_id:
            raise ValidationError("H3 task 仍在运行，请先继续等待，不能创建重复任务")
        latest = self._latest_attempt(segment)
        if refresh_prompt:
            if latest is None or latest.status not in {
                NodeExecutionStatus.FAILED,
                NodeExecutionStatus.COMPLETED,
            }:
                raise ValidationError("只有已有终态结果的 H3 片段可以刷新提示词")
            self._refresh_segment_prompt(context, segment)
        elif latest is None or latest.status != NodeExecutionStatus.FAILED:
            raise ValidationError("只有失败的 H3 片段可以重试")
        context.last_error = None
        return self._run_segment(context, segment)

    retry_h3 = retry_segment

    def finalize(self, job_id: str) -> PipelineResult:
        """Concatenate all completed long-video segment outputs locally."""

        context, _ = self._load_existing_context(job_id)
        if context.stage == PipelineStage.COMPLETED:
            return self._result(context)
        if not context.h3_segments:
            raise ValidationError("还没有可拼接的 H3 片段")
        active = [
            segment
            for segment in context.h3_segments
            if self._active_attempt(segment) is not None
        ]
        if active:
            raise ValidationError("仍有 H3 task 运行中，请先继续等待")
        incomplete = [
            segment.index
            for segment in context.h3_segments
            if segment.status != "completed" or not segment.output_artifact
        ]
        if incomplete:
            raise ValidationError(f"片段尚未完成，不能拼接：segment {incomplete[0]}")

        sources = [
            self._absolute_path(context, segment.output_artifact)
            for segment in sorted(context.h3_segments, key=lambda item: item.index)
        ]
        output = context.job_dir / "output" / f"final_{context.spec.target_locale}.mp4"
        total_duration = sum(
            segment.normalized_duration_seconds for segment in context.h3_segments
        )
        try:
            final_path = concat_videos(
                [Path(item) for item in sources if item is not None],
                output,
                target_duration_seconds=float(total_duration),
                ffprobe_bin=self.config.ffprobe_bin,
                ffmpeg_bin=self.ffmpeg_bin,
                timeout=max(self.config.http_timeout, 900.0),
            )
            info = ffprobe.probe(
                final_path,
                ffprobe_bin=self.config.ffprobe_bin,
                timeout=self.config.http_timeout,
            )
            self._validate_output_info(info, expected_duration=float(total_duration))
        except Exception as exc:  # noqa: BLE001 - local finalization is auditable
            self._fail_job(context, PipelineStage.WAITING_FOR_NEXT_SEGMENT, exc)
            raise self._normalize_exception(exc) from exc

        info_path = context.job_dir / "json" / "final_info.json"
        write_json(info_path, info.raw)
        self._set_artifact(context, "final_info", info_path)
        self._set_artifact(context, "final_video", final_path)
        context.stage = PipelineStage.COMPLETED
        context.progress = 100
        context.last_error = None
        context.metrics["final_duration_seconds"] = round(info.duration, 3)
        self._save_checkpoint(context)
        self._emit(context, "completed", "H3 long-video segments concatenated", output=str(final_path))
        return self._result(context)

    finish = finalize

    def resume_failed(self, job_id: str, *, spec: JobSpec | None = None) -> PipelineResult:
        """Dispatch the safe explicit recovery operation used by the UI."""

        context, _ = self._load_existing_context(job_id)
        if spec is not None and spec.target_locale != context.spec.target_locale:
            raise ValidationError("retry spec target locale does not match checkpoint")
        segment = self._latest_incomplete_segment(context)
        if segment is None:
            return self._result(context)
        active = self._active_attempt(segment)
        if active is not None and active.task_id:
            return self.continue_segment(job_id, segment.index)
        latest = self._latest_attempt(segment)
        if latest is not None and latest.status == NodeExecutionStatus.FAILED:
            return self.retry_segment(job_id, segment.index)
        raise ValidationError("checkpoint does not identify a resumable H3 operation")

    # ---------- job preparation ----------

    def _prepare_new_context(
        self,
        spec: JobSpec,
        *,
        job_id: str | None,
        execution_mode: ExecutionMode,
    ) -> JobContext:
        self._validate_job_spec(spec)
        resolved_id = job_id or spec.job_id or new_job_id()
        self._validate_job_id(resolved_id)
        job_dir = (self.config.work_dir / resolved_id).resolve()
        checkpoint = job_dir / "checkpoint.json"
        if checkpoint.is_file():
            raise ValidationError(f"Job {resolved_id} already exists; use its history actions")

        context = JobContext(
            pipeline_version=PIPELINE_VERSION,
            job_id=resolved_id,
            job_dir=job_dir,
            spec=spec.model_copy(update={"job_id": resolved_id}),
            provider=H3_PROVIDER,
            execution_mode=execution_mode,
            created_at=_now_iso(),
            updated_at=_now_iso(),
            stage=PipelineStage.PREPARING,
        )
        for relative in (
            "input",
            "input/segments",
            "input/source_segments",
            "input/references",
            "input/storyboard",
            "json/raw",
            "json/nodes/doubao",
            "json/nodes/seedream",
            "json/nodes/h3",
            "output",
        ):
            (job_dir / relative).mkdir(parents=True, exist_ok=True)

        source = Path(spec.input_video)
        source_copy = job_dir / "input" / f"source_master{source.suffix.lower() or '.mp4'}"
        shutil.copy2(source, source_copy)
        context.source_master_artifact = self._path_reference(context, source_copy)
        self._set_artifact(context, "source_master", source_copy)

        # v7 references are selected from the complete source video by
        # Doubao and generated by Seedream.  Keep an empty legacy artifact so
        # old history renderers can still display a stable path, but never
        # copy or upload user-provided reference images in the active flow.
        references: list[dict[str, str]] = []
        references_path = job_dir / "json" / "references.json"
        write_json(references_path, references)
        self._set_artifact(context, "references", references_path)
        context.cache_key = self._build_cache_key(
            source_copy,
            spec,
            references,
            base_dir=job_dir,
        )
        self._save_checkpoint(context)
        return context

    # Compatibility helper used by callers that previously prepared a context
    # through core.pipeline. It intentionally only returns the new H3 context.
    def _prepare_context(
        self,
        spec: JobSpec,
        *,
        job_id: str | None = None,
        execution_mode: ExecutionMode | str = ExecutionMode.MANUAL,
    ) -> tuple[JobContext, dict[str, Any]]:
        resolved_id = job_id or spec.job_id
        if resolved_id and (self.config.work_dir / resolved_id / "checkpoint.json").is_file():
            context, state = self._load_existing_context(resolved_id)
            return context, state
        try:
            mode = ExecutionMode(execution_mode)
        except ValueError as exc:
            raise ValidationError("execution_mode must be 'manual' or 'auto'") from exc
        context = self._prepare_new_context(spec, job_id=resolved_id, execution_mode=mode)
        return context, {"source_master": self._source_master_path(context)}

    def _validate_job_spec(self, spec: JobSpec) -> None:
        if not spec.input_video or not Path(spec.input_video).is_file():
            raise ValidationError(f"Input video does not exist: {spec.input_video}")
        references = self._reference_paths(spec)
        if references:
            raise ValidationError(
                "v7 不接受用户参考图；参考图由 Doubao 分镜分析后交给 Seedream 生成"
            )
        if not spec.target_language:
            raise ValidationError("H3 target language is required")
        from language_config import is_h3_native_language

        if not is_h3_native_language(spec.target_language):
            raise ValidationError(
                f"MiniMax H3 当前只开放稳定对白语言，暂不支持 {spec.target_language!r}"
            )

    @staticmethod
    def _reference_paths(spec: JobSpec) -> list[Path]:
        result: list[Path] = []
        for path in [*spec.reference_images, *spec.character_refs, *spec.scene_refs]:
            path = Path(path).expanduser()
            if path not in result:
                result.append(path)
        return result

    @staticmethod
    def _validate_job_id(job_id: str) -> None:
        if not job_id or job_id in {".", ".."} or Path(job_id).name != job_id:
            raise ValidationError("Invalid job ID")

    def _copy_uploaded_segment(self, context: JobContext, source: Path, index: int) -> Path:
        destination = (
            context.job_dir
            / "input"
            / "source_segments"
            / f"segment_{index:03d}{source.suffix.lower() or '.mp4'}"
        )
        shutil.copy2(source, destination)
        return destination

    def _add_segment_from_path(self, context: JobContext, path: Path, *, index: int) -> H3Segment:
        expected_index = len(context.h3_segments) + 1
        if index != expected_index:
            raise ValidationError(f"片段必须按顺序上传，当前需要 segment {expected_index}")
        info = ffprobe.probe(
            Path(path),
            ffprobe_bin=self.config.ffprobe_bin,
            timeout=self.config.http_timeout,
        )
        if not info.has_video:
            raise ValidationError("segment video must contain a video stream")
        if info.duration < H3_MIN_DURATION_SECONDS:
            raise ValidationError(
                f"segment {index} is {info.duration:.3f}s; each H3 slice must be at least 4 seconds"
            )
        if info.duration > H3_MAX_DURATION_SECONDS:
            raise ValidationError(
                f"segment {index} is {info.duration:.3f}s; each H3 slice cannot exceed 15 seconds"
            )
        normalized_duration = _round_duration(info.duration)
        destination = context.job_dir / "input" / "segments" / f"segment_{index:03d}.mp4"
        normalize_video(
            Path(path),
            destination,
            duration_seconds=normalized_duration,
            source_duration_seconds=info.duration,
            ffprobe_bin=self.config.ffprobe_bin,
            ffmpeg_bin=self.ffmpeg_bin,
            timeout=max(self.config.http_timeout, 600.0),
        )
        package = self._load_analysis_package(context)
        source_start_ms = int(round(sum(item.source_duration_seconds for item in context.h3_segments) * 1000))
        source_end_ms = int(round((source_start_ms / 1000.0 + info.duration) * 1000))
        master_duration = self._source_master_duration(context)
        if source_end_ms > int(round(master_duration * 1000)) + 500:
            raise ValidationError(
                f"segment {index} exceeds the analyzed source timeline; check ordered slices"
            )
        reference_shots = self._reference_shots_for_range(
            context,
            source_start_ms,
            source_end_ms,
        )
        if not reference_shots:
            raise ValidationError(
                f"segment {index} has no matching Seedream storyboard reference"
            )
        if len(reference_shots) > 9:
            raise ValidationError(
                f"segment {index} covers {len(reference_shots)} shots; split it so H3 receives at most 9 reference images"
            )
        reference_map = self._reference_shot_map(reference_shots, source_start_ms)
        prompt = build_transformation_prompt(
            target_language=context.spec.target_language,
            target_region=context.spec.target_region,
            target_locale=context.spec.target_locale,
            transformation_instruction=context.spec.transformation_instruction,
            localization_prompt=package.h3_prompt,
            segment_index=index,
            has_seedream_references=True,
            reference_shot_map=reference_map,
        )
        segment = H3Segment(
            index=index,
            source_duration_seconds=round(info.duration, 3),
            normalized_duration_seconds=normalized_duration,
            prompt=prompt,
            source_artifact=self._path_reference(context, destination),
            source_start_ms=source_start_ms,
            source_end_ms=source_end_ms,
            reference_shot_ids=[shot.shot_id for shot in reference_shots],
            reference_strategy="seedream_storyboard",
            status="pending",
        )
        context.h3_segments.append(segment)
        context.stage = PipelineStage.GENERATING_SEGMENT
        context.progress = self._segment_progress(index)
        self._write_segments_manifest(context)
        self._save_checkpoint(context)
        return segment

    def _refresh_segment_prompt(self, context: JobContext, segment: H3Segment) -> None:
        """Build and persist the current H3 prompt without calling Doubao."""

        package = self._load_analysis_package(context)
        segment.prompt = build_transformation_prompt(
            target_language=context.spec.target_language,
            target_region=context.spec.target_region,
            target_locale=context.spec.target_locale,
            transformation_instruction=context.spec.transformation_instruction,
            localization_prompt=package.h3_prompt,
            segment_index=segment.index,
            has_seedream_references=True,
            reference_shot_map=self._reference_shot_map_for_segment(context, segment),
        )
        context.metrics["h3_prompt_version"] = H3_PROMPT_VERSION
        self._write_segments_manifest(context)
        self._save_checkpoint(context)

    # ---------- H3 execution ----------

    def _run_segment(self, context: JobContext, segment: H3Segment) -> PipelineResult:
        self._ensure_clients()
        if segment.index > 1:
            previous = self._previous_segment(context, segment.index)
            if previous is None or previous.status != "completed" or not previous.output_artifact:
                raise ValidationError("上一片 H3 输出未完成，不能处理当前片段")
        self._set_stage(context, PipelineStage.GENERATING_SEGMENT)
        attempt_number = max((item.attempt for item in segment.attempts), default=0) + 1
        attempt = H3Attempt(
            attempt=attempt_number,
            status=NodeExecutionStatus.RUNNING,
            started_at=_now_iso(),
        )
        segment.attempts.append(attempt)
        segment.active_attempt = attempt_number
        segment.status = "running"
        node = NodeExecution(
            node=H3_NODE,
            attempt=attempt_number,
            status=NodeExecutionStatus.RUNNING,
            provider=H3_PROVIDER,
            segment_index=segment.index,
            started_at=attempt.started_at,
        )
        context.node_executions.append(node)
        self._save_checkpoint(context)
        self._emit(
            context,
            "node_started",
            f"H3 segment {segment.index} attempt {attempt_number} started",
            node=H3_NODE,
            segment_index=segment.index,
            attempt=attempt_number,
        )
        try:
            content = self._prepare_h3_request(context, segment, attempt, node)
            self._check_cancel()
            task = self.minimax_client.create_task(
                content,
                duration=segment.normalized_duration_seconds,
                resolution=self.config.minimax_resolution,
                ratio="adaptive",
                raw_dir=self._attempt_raw_dir(context, segment, attempt),
            )
            task_id = self._task_value(task, "task_id")
            request_id = self._task_value(task, "request_id")
            if not task_id:
                raise ProviderError(
                    "MiniMax H3 create response has no task ID",
                    provider=H3_PROVIDER,
                    error_code="TASK_ID_MISSING",
                    retryable=False,
                )
            attempt.task_id = str(task_id)
            attempt.request_id = str(request_id) if request_id else None
            create_raw = getattr(task, "raw_path", None)
            if create_raw:
                attempt.create_response_artifact = self._path_reference(context, Path(create_raw))
            node.task_id = str(task_id)
            if request_id:
                node.request_ids.append(str(request_id))
            context.task_ids[f"h3_segment_{segment.index}"] = str(task_id)
            if request_id:
                context.request_ids[f"h3_segment_{segment.index}"] = str(request_id)
            self._save_checkpoint(context)  # task ID is durable before polling
            self._emit(
                context,
                "task",
                f"H3 segment {segment.index} task created",
                segment_index=segment.index,
                attempt=attempt.attempt,
                task_id=task_id,
                request_id=request_id,
            )
            return self._wait_and_finalize_segment(context, segment, attempt)
        except Exception as exc:  # noqa: BLE001 - preserve paid-attempt evidence
            self._handle_attempt_error(context, segment, attempt, exc, node=node)
            raise self._normalize_exception(exc) from exc

    def _prepare_h3_request(
        self,
        context: JobContext,
        segment: H3Segment,
        attempt: H3Attempt,
        node: NodeExecution,
    ) -> list[dict[str, Any]]:
        source_path = self._absolute_path(context, segment.source_artifact)
        if source_path is None or not source_path.is_file():
            raise ValidationError("H3 segment source artifact is missing")
        source_asset = self._ensure_asset(
            context,
            segment.source_asset,
            source_path,
            kind=f"h3_segment_{segment.index}_source",
        )
        segment.source_asset = source_asset
        reference_assets = self._ensure_seedream_assets_for_segment(context, segment)
        segment.reference_assets = reference_assets

        if attempt.attempt == 1:
            package = self._load_analysis_package(context)
            prompt = build_transformation_prompt(
                target_language=context.spec.target_language,
                target_region=context.spec.target_region,
                target_locale=context.spec.target_locale,
                transformation_instruction=context.spec.transformation_instruction,
                localization_prompt=package.h3_prompt,
                segment_index=segment.index,
                has_seedream_references=True,
                reference_shot_map=self._reference_shot_map_for_segment(context, segment),
            )
        else:
            # A H3 retry reuses the exact persisted prompt and never asks
            # Doubao to reinterpret the source a second time.
            prompt = segment.prompt
        segment.prompt = prompt
        content = build_h3_content(
            source_asset.remote_url,
            prompt,
            reference_assets=reference_assets,
        )
        attempt_dir = self._attempt_dir(context, segment, attempt)
        content_path = attempt_dir / "content.json"
        write_json(content_path, content)
        attempt.content_artifact = self._path_reference(context, content_path)
        attempt.input_artifacts = [
            self._path_reference(context, source_path),
            *[self._path_reference(context, asset.local_path) for asset in reference_assets],
        ]
        node.input_artifacts = list(attempt.input_artifacts)
        node.output_artifacts.append(self._path_reference(context, content_path))
        self._write_segments_manifest(context)
        self._save_checkpoint(context)
        return content

    def _wait_and_finalize_segment(
        self,
        context: JobContext,
        segment: H3Segment,
        attempt: H3Attempt,
    ) -> PipelineResult:
        if not attempt.task_id:
            raise ValidationError("H3 attempt has no task ID")
        node = self._node_for_attempt(context, segment.index, attempt.attempt)
        response = self.minimax_client.wait_task(
            attempt.task_id,
            raw_dir=self._attempt_raw_dir(context, segment, attempt),
            cancel_event=self.cancel_event,
        )
        response_path = getattr(response, "raw_path", None)
        if response_path:
            poll_artifact = self._path_reference(context, Path(response_path))
            if poll_artifact not in attempt.poll_response_artifacts:
                attempt.poll_response_artifacts.append(poll_artifact)
        data = response.data if hasattr(response, "data") else response
        final_response_path = self._attempt_dir(context, segment, attempt) / "final_response.json"
        write_json(final_response_path, data)
        attempt.final_response_artifact = self._path_reference(context, final_response_path)
        if node is not None and attempt.final_response_artifact not in node.output_artifacts:
            node.output_artifacts.append(attempt.final_response_artifact)
        self._save_checkpoint(context)
        video_url = task_video_url(data)
        if not video_url:
            raise ProviderError(
                "MiniMax H3 succeeded without task.content.url",
                provider=H3_PROVIDER,
                error_code="VIDEO_URL_MISSING",
                request_id=getattr(response, "request_id", None),
                payload=data,
                retryable=False,
            )
        attempt.video_url = str(video_url)
        provider_output_path = self._attempt_dir(context, segment, attempt) / "provider_output.mp4"
        download(
            str(video_url),
            provider_output_path,
            timeout=self.config.http_timeout,
            attempts=self.config.max_retries,
        )
        if not provider_output_path.is_file() or provider_output_path.stat().st_size <= 0:
            raise ProviderError(
                "MiniMax H3 produced no output video file",
                provider=H3_PROVIDER,
                error_code="EMPTY_VIDEO_RESULT",
                payload=data,
                retryable=False,
            )
        provider_info = ffprobe.probe(
            provider_output_path,
            ffprobe_bin=self.config.ffprobe_bin,
            timeout=self.config.http_timeout,
        )
        self._validate_output_info(provider_info, expected_duration=None)
        provider_info_path = self._attempt_dir(context, segment, attempt) / "provider_output_info.json"
        write_json(provider_info_path, provider_info.raw)
        attempt.provider_output_artifact = self._path_reference(context, provider_output_path)
        if node is not None:
            for artifact in (
                attempt.provider_output_artifact,
                self._path_reference(context, provider_info_path),
            ):
                if artifact not in node.output_artifacts:
                    node.output_artifacts.append(artifact)
        self._save_checkpoint(context)

        output_path = self._attempt_dir(context, segment, attempt) / "output.mp4"
        expected_duration = float(segment.normalized_duration_seconds)
        if abs(float(provider_info.duration) - expected_duration) > H3_OUTPUT_DURATION_TOLERANCE:
            normalize_video(
                provider_output_path,
                output_path,
                duration_seconds=segment.normalized_duration_seconds,
                source_duration_seconds=provider_info.duration,
                ffprobe_bin=self.config.ffprobe_bin,
                ffmpeg_bin=self.ffmpeg_bin,
                timeout=max(self.config.http_timeout, 600.0),
            )
        else:
            shutil.copy2(provider_output_path, output_path)
        info = ffprobe.probe(
            output_path,
            ffprobe_bin=self.config.ffprobe_bin,
            timeout=self.config.http_timeout,
        )
        self._validate_output_info(info, expected_duration=expected_duration)
        info_path = self._attempt_dir(context, segment, attempt) / "output_info.json"
        write_json(info_path, info.raw)
        attempt.output_artifact = self._path_reference(context, output_path)
        segment.output_artifact = attempt.output_artifact
        attempt.finished_at = _now_iso()
        attempt.status = NodeExecutionStatus.COMPLETED
        segment.active_attempt = None
        segment.status = "completed"
        if node is not None:
            node.status = NodeExecutionStatus.COMPLETED
            node.finished_at = attempt.finished_at
            for artifact in (
                attempt.output_artifact,
                self._path_reference(context, final_response_path),
                self._path_reference(context, info_path),
            ):
                if artifact not in node.output_artifacts:
                    node.output_artifacts.append(artifact)

        self._write_segments_manifest(context)
        if context.source_master_duration_seconds is not None and context.source_master_duration_seconds <= H3_MAX_DURATION_SECONDS:
            final_video = context.job_dir / "output" / f"final_{context.spec.target_locale}.mp4"
            shutil.copy2(output_path, final_video)
            self._set_artifact(context, "final_video", final_video)
            stable_info = context.job_dir / "json" / "final_info.json"
            write_json(stable_info, info.raw)
            self._set_artifact(context, "final_info", stable_info)
            context.stage = PipelineStage.COMPLETED
            context.progress = 100
            context.metrics["final_duration_seconds"] = round(info.duration, 3)
            message = "H3 转化完成"
            action = None
        else:
            context.stage = PipelineStage.WAITING_FOR_NEXT_SEGMENT
            context.progress = 35
            message = "当前片段已完成，请按顺序上传下一片；全部片段完成后再拼接"
            action = "append_segment"
        context.last_error = None
        self._save_checkpoint(context)
        self._emit(
            context,
            "node_completed",
            f"H3 segment {segment.index} completed",
            node=H3_NODE,
            segment_index=segment.index,
            attempt=attempt.attempt,
            output=str(output_path),
        )
        if context.stage == PipelineStage.COMPLETED:
            self._emit(context, "completed", message, output=str(context.job_dir / "output"))
        return self._result(
            context,
            action_required=action,
            segment_index=segment.index,
            next_segment_index=(segment.index + 1 if action else None),
            message=message,
        )

    def _handle_attempt_error(
        self,
        context: JobContext,
        segment: H3Segment,
        attempt: H3Attempt,
        exc: Exception,
        *,
        node: NodeExecution | None = None,
    ) -> None:
        record = self._error_record(PipelineStage.GENERATING_SEGMENT, exc)
        if isinstance(exc, ProviderError):
            if exc.request_id and not attempt.request_id:
                attempt.request_id = exc.request_id
            if exc.raw_response_path and attempt.task_id:
                raw_artifact = self._path_reference(context, Path(exc.raw_response_path))
                if raw_artifact not in attempt.poll_response_artifacts:
                    attempt.poll_response_artifacts.append(raw_artifact)
            if node is None:
                node = self._node_for_attempt(context, segment.index, attempt.attempt)
            if node is not None and exc.request_id and exc.request_id not in node.request_ids:
                node.request_ids.append(exc.request_id)
        keep_running = bool(attempt.task_id) and self._is_recoverable_active_error(exc)
        if keep_running:
            attempt.error = record.as_dict()
            segment.status = "running"
            context.stage = PipelineStage.GENERATING_SEGMENT
            context.last_error = record.as_dict()
            self._save_checkpoint(context)
            self._emit(context, "error", record.message, error=record.as_dict())
            return

        attempt.status = NodeExecutionStatus.FAILED
        attempt.finished_at = _now_iso()
        attempt.error = record.as_dict()
        segment.active_attempt = None
        segment.status = "failed"
        failure_path = self._attempt_dir(context, segment, attempt) / "failure.json"
        write_json(
            failure_path,
            {
                "error": record.as_dict(),
                "provider_payload": getattr(exc, "payload", None),
            },
        )
        attempt.failure_artifact = self._path_reference(context, failure_path)
        if node is None:
            node = self._node_for_attempt(context, segment.index, attempt.attempt)
        if node is not None:
            node.status = NodeExecutionStatus.FAILED
            node.finished_at = attempt.finished_at
            node.error = record.as_dict()
            if attempt.failure_artifact not in node.output_artifacts:
                node.output_artifacts.append(attempt.failure_artifact)
        context.stage = PipelineStage.FAILED
        context.progress = 0
        context.last_error = record.as_dict()
        context.task_ids.pop(f"h3_segment_{segment.index}", None)
        context.request_ids.pop(f"h3_segment_{segment.index}", None)
        self._write_segments_manifest(context)
        self._save_checkpoint(context)
        self._emit(
            context,
            "node_failed",
            record.message,
            node=H3_NODE,
            segment_index=segment.index,
            attempt=attempt.attempt,
            error=record.as_dict(),
        )

    @staticmethod
    def _is_recoverable_active_error(exc: Exception) -> bool:
        if isinstance(exc, PipelineCancelled):
            return True
        if not isinstance(exc, ProviderError):
            return False
        return (
            exc.error_code == "TASK_TIMEOUT"
            or bool(exc.retryable)
            or exc.error_code not in H3_TERMINAL_ERROR_CODES
        )

    def _mark_unknown_running_attempt(
        self,
        context: JobContext,
        segment: H3Segment,
        attempt: H3Attempt,
        message: str,
    ) -> None:
        record = ErrorRecord(
            stage=PipelineStage.GENERATING_SEGMENT.value,
            message=message,
            provider=H3_PROVIDER,
            error_code="CREATE_OUTCOME_UNKNOWN",
            retryable=False,
        ).as_dict()
        attempt.status = NodeExecutionStatus.FAILED
        attempt.finished_at = _now_iso()
        attempt.error = record
        segment.active_attempt = None
        segment.status = "failed"
        context.stage = PipelineStage.FAILED
        context.last_error = record
        self._save_checkpoint(context)

    # ---------- assets and persistence ----------

    def _ensure_clients(self) -> None:
        if self.ark_client is None:
            self.ark_client = ArkClient(self.config, logger=self._logger)
        if self.minimax_client is None:
            self.minimax_client = MiniMaxClient(self.config, logger=self._logger)
        if self.uguu_client is None:
            self.uguu_client = UguuClient(self.config, logger=self._logger)

    def _ensure_reference_assets(self, context: JobContext, segment: H3Segment) -> list[UploadedAsset]:
        """Legacy v6 helper retained for compatibility-only callers."""

        references = self._context_reference_paths(context)
        existing = {str(asset.local_path.resolve()): asset for asset in segment.reference_assets}
        assets: list[UploadedAsset] = []
        for path in references:
            previous = existing.get(str(path.resolve()))
            assets.append(
                self._ensure_asset(
                    context,
                    previous,
                    path,
                    kind="user_reference_image",
                )
            )
        return assets

    def _ensure_seedream_assets_for_segment(
        self,
        context: JobContext,
        segment: H3Segment,
    ) -> list[UploadedAsset]:
        if not segment.reference_shot_ids:
            raise ValidationError(f"segment {segment.index} has no Seedream reference IDs")
        references: list[UploadedAsset] = []
        for shot_id in segment.reference_shot_ids:
            reference = next(
                (item for item in context.seedream_references if item.shot_id == shot_id),
                None,
            )
            if reference is None:
                raise ValidationError(f"Seedream reference {shot_id} is missing")
            output_path = self._absolute_path(context, reference.output_artifact)
            if output_path is None or not output_path.is_file():
                raise ValidationError(f"Seedream reference output is missing: {shot_id}")
            inspect_image(output_path)
            asset = self._ensure_asset(
                context,
                reference.reference_asset,
                output_path,
                kind=f"seedream_shot_{shot_id}_reference",
            )
            reference.reference_asset = asset
            references.append(asset)
        return references

    def _ensure_asset(
        self,
        context: JobContext,
        asset: UploadedAsset | None,
        path: Path,
        *,
        kind: str,
    ) -> UploadedAsset:
        if asset is not None and Path(asset.local_path).resolve() == Path(path).resolve() and self._asset_is_fresh(asset):
            return asset
        self._ensure_clients()
        return self.uguu_client.upload(
            Path(path),
            kind=kind,
            raw_dir=self._raw_dir(context),
        )

    def _asset_is_fresh(self, asset: UploadedAsset) -> bool:
        try:
            uploaded = datetime.fromisoformat(asset.uploaded_at.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return False
        if uploaded.tzinfo is None:
            uploaded = uploaded.replace(tzinfo=timezone.utc)
        if asset.expires_at:
            try:
                expires = datetime.fromisoformat(asset.expires_at.replace("Z", "+00:00"))
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                return datetime.now(timezone.utc) < expires
            except (TypeError, ValueError):
                pass
        age = (datetime.now(timezone.utc) - uploaded).total_seconds()
        return 0 <= age < self.config.uguu_expire_hours * 3600

    @staticmethod
    def _find_asset(assets: list[UploadedAsset], path: Path) -> UploadedAsset | None:
        resolved = path.resolve()
        return next(
            (asset for asset in assets if Path(asset.local_path).resolve() == resolved),
            None,
        )

    def _context_reference_paths(self, context: JobContext) -> list[Path]:
        reference_path = self._artifact_path(context, "references", required=False)
        if reference_path is None or not reference_path.is_file():
            return []
        value = read_json(reference_path)
        if not isinstance(value, list):
            raise ValidationError("checkpoint references artifact is invalid")
        paths: list[Path] = []
        for item in value:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise ValidationError("checkpoint references artifact has an invalid entry")
            path = self._absolute_path(context, item["path"])
            if path is None or not path.is_file():
                raise ValidationError("checkpoint reference image is missing")
            paths.append(path)
        return paths

    def _write_segments_manifest(self, context: JobContext) -> None:
        path = context.job_dir / "json" / "h3_segments.json"
        write_json(path, [item.model_dump(mode="json") for item in context.h3_segments])
        self._set_artifact(context, "h3_segments", path)

    def _record_master_info(self, context: JobContext, raw: dict[str, Any], duration: float) -> None:
        path = context.job_dir / "json" / "source_master_info.json"
        write_json(path, raw)
        self._set_artifact(context, "source_master_info", path)
        context.source_master_duration_seconds = round(duration, 3)
        context.metrics["source_master_duration_seconds"] = round(duration, 3)
        self._save_checkpoint(context)

    def _source_master_duration(self, context: JobContext) -> float:
        if context.source_master_duration_seconds is not None:
            return float(context.source_master_duration_seconds)
        master = self._source_master_path(context)
        info = ffprobe.probe(
            master,
            ffprobe_bin=self.config.ffprobe_bin,
            timeout=self.config.http_timeout,
        )
        if not info.has_video:
            raise ValidationError("input video must contain a video stream")
        self._record_master_info(context, info.raw, info.duration)
        return float(info.duration)

    def _load_analysis_package(
        self,
        context: JobContext,
        *,
        required: bool = True,
    ) -> LocalizationPackage | None:
        package_path = self._artifact_path(context, "localization_package", required=False)
        if package_path is None:
            fallback = context.job_dir / "json" / "localization_package.json"
            if fallback.is_file():
                package_path = fallback
                self._set_artifact(context, "localization_package", fallback)
        if package_path is None or not package_path.is_file():
            if required:
                raise ValidationError("Doubao localization package is missing")
            return None
        try:
            package = validate_localization_package(
                LocalizationPackage.model_validate(read_json(package_path)),
                target_language=context.spec.target_language,
                target_region=context.spec.target_region,
                target_locale=context.spec.target_locale,
                duration_seconds=self._source_master_duration(context),
                transformation_instruction=context.spec.transformation_instruction,
                require_h3_prompt=True,
                require_reference_plan=True,
            )
        except Exception as exc:  # noqa: BLE001 - normalize damaged checkpoints
            if not required:
                return None
            raise ValidationError(f"Invalid Doubao localization package: {exc}") from exc
        prompt_path = self._artifact_path(context, "doubao_h3_prompt", required=False)
        if prompt_path is None:
            fallback_prompt = context.job_dir / "json" / "doubao_h3_prompt.txt"
            if fallback_prompt.is_file():
                prompt_path = fallback_prompt
                self._set_artifact(context, "doubao_h3_prompt", fallback_prompt)
        if prompt_path is None or not prompt_path.is_file():
            if required:
                raise ValidationError("Doubao H3 prompt artifact is missing")
            return None
        try:
            persisted_prompt = prompt_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            if required:
                raise ValidationError(f"Doubao H3 prompt artifact cannot be read: {exc}") from exc
            return None
        if persisted_prompt != package.h3_prompt:
            if required:
                raise ValidationError(
                    "Doubao H3 prompt artifact does not match localization package"
                )
            return None
        return package

    def _recover_analysis_completion(self, context: JobContext) -> bool:
        """Promote a durably-written package without calling Doubao again."""

        try:
            package = self._load_analysis_package(context, required=False)
        except ValidationError:
            return False
        node = self._latest_node(context, DOUBAO_NODE)
        if package is None or node is None:
            return False
        if node.status != NodeExecutionStatus.COMPLETED:
            # A stable package can outlive a later failed/restarted attempt.
            # Only recover an unfinished node when that exact attempt also
            # persisted its validated package and prompt. This prevents an
            # old successful analysis from being silently reused after a new
            # Doubao call failed before producing a result.
            attempt_dir = self._doubao_attempt_dir(context, node.attempt)
            attempt_package_path = attempt_dir / "package.json"
            attempt_prompt_path = attempt_dir / "h3_prompt.txt"
            if not attempt_package_path.is_file() or not attempt_prompt_path.is_file():
                return False
            try:
                attempt_package = validate_localization_package(
                    LocalizationPackage.model_validate(read_json(attempt_package_path)),
                    target_language=context.spec.target_language,
                    target_region=context.spec.target_region,
                    target_locale=context.spec.target_locale,
                    duration_seconds=self._source_master_duration(context),
                    transformation_instruction=context.spec.transformation_instruction,
                    require_h3_prompt=True,
                    require_reference_plan=True,
                )
                attempt_prompt = attempt_prompt_path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError, ValidationError, ValueError, TypeError):
                return False
            if attempt_package.model_dump(mode="json") != package.model_dump(mode="json"):
                return False
            if attempt_prompt != package.h3_prompt:
                return False
            self._finish_node(
                context,
                node,
                NodeExecutionStatus.COMPLETED,
                output_artifacts=self._analysis_output_artifacts(context, node.attempt),
            )
        context.metrics.setdefault("speaker_count", len(package.speakers))
        context.metrics.setdefault("dialogue_count", len(package.dialogues))
        context.metrics.setdefault("analysis_prompt_version", ANALYSIS_PROMPT_VERSION)
        self._save_checkpoint(context)
        return True

    def _recover_saved_doubao_package(
        self,
        context: JobContext,
    ) -> LocalizationPackage | None:
        """Explicitly recover a known malformed, user-approved Doubao result.

        This is deliberately separate from restart recovery.  A normal
        restart never treats a malformed model response as a valid package;
        the explicit approval action may promote the narrow schema-wrapper
        shape that was already shown to and accepted by the user, without a
        second cloud call.
        """

        node = self._latest_node(context, DOUBAO_NODE)
        if node is None or node.status != NodeExecutionStatus.FAILED:
            return None
        for provider_call in reversed(node.provider_calls):
            raw_path = self._absolute_path(context, provider_call.raw_response_path)
            if raw_path is None or not raw_path.is_file():
                continue
            content = self._read_saved_doubao_content(raw_path)
            if content is None:
                continue
            candidate = recover_doubao_schema_wrapper(content)
            if candidate is None:
                continue
            try:
                package = validate_localization_package(
                    candidate,
                    target_language=context.spec.target_language,
                    target_region=context.spec.target_region,
                    target_locale=context.spec.target_locale,
                    duration_seconds=self._source_master_duration(context),
                    transformation_instruction=context.spec.transformation_instruction,
                    require_h3_prompt=True,
                    require_reference_plan=True,
                )
            except ValidationError:
                continue

            attempt_dir = self._doubao_attempt_dir(context, node.attempt)
            package_path = context.job_dir / "json" / "localization_package.json"
            prompt_path = context.job_dir / "json" / "doubao_h3_prompt.txt"
            attempt_package_path = attempt_dir / "package.json"
            attempt_prompt_path = attempt_dir / "h3_prompt.txt"
            recovery_path = attempt_dir / "recovery.json"
            package_data = package.model_dump(mode="json")
            write_json(attempt_package_path, package_data)
            attempt_prompt_path.write_text(f"{package.h3_prompt}\n", encoding="utf-8")
            write_json(package_path, package_data)
            prompt_path.write_text(f"{package.h3_prompt}\n", encoding="utf-8")
            write_json(
                recovery_path,
                {
                    "method": "explicit_user_approved_schema_wrapper_recovery",
                    "source_raw_response_path": self._path_reference(context, raw_path),
                    "ignored_trailing_field": "audio_path",
                    "video_analysis_coercion": "string_to_summary_object",
                    "recovered_at": _now_iso(),
                },
            )
            self._set_artifact(context, "localization_package", package_path)
            self._set_artifact(context, "doubao_h3_prompt", prompt_path)
            context.metrics["speaker_count"] = len(package.speakers)
            context.metrics["dialogue_count"] = len(package.dialogues)
            context.metrics["analysis_prompt_version"] = ANALYSIS_PROMPT_VERSION
            context.metrics["doubao_recovery"] = "explicit_schema_wrapper"
            context.last_error = None
            self._finish_node(
                context,
                node,
                NodeExecutionStatus.COMPLETED,
                output_artifacts=[
                    self._path_reference(context, attempt_package_path),
                    self._path_reference(context, attempt_prompt_path),
                    self._path_reference(context, package_path),
                    self._path_reference(context, prompt_path),
                    self._path_reference(context, recovery_path),
                ],
            )
            self._emit(
                context,
                "doubao_recovered",
                "已使用用户确认的 Doubao 结构化结果，不重复调用 Doubao",
                attempt=node.attempt,
                source_raw_response_path=self._path_reference(context, raw_path),
                recovery_path=self._path_reference(context, recovery_path),
            )
            return package
        return None

    @staticmethod
    def _read_saved_doubao_content(path: Path) -> str | None:
        try:
            payload = read_json(path)
        except (OSError, UnicodeError, ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        first = choices[0]
        if not isinstance(first, dict):
            return None
        message = first.get("message")
        if not isinstance(message, dict):
            return None
        content = message.get("content")
        return content if isinstance(content, str) else None

    def _analysis_output_artifacts(self, context: JobContext, attempt: int) -> list[str]:
        package_path = self._artifact_path(context, "localization_package", required=False)
        prompt_path = self._artifact_path(context, "doubao_h3_prompt", required=False)
        source_asset_path = self._artifact_path(context, "doubao_source_asset", required=False)
        attempt_dir = self._doubao_attempt_dir(context, attempt)
        paths = [
            attempt_dir / "package.json",
            attempt_dir / "h3_prompt.txt",
        ]
        for path in (source_asset_path, package_path, prompt_path):
            if path is not None:
                paths.append(path)
        return [self._path_reference(context, path) for path in paths if path.is_file()]

    def _begin_node(
        self,
        context: JobContext,
        node_name: str,
        *,
        provider: str,
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
            provider=provider,
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
            raw_response_path=str(raw_response_path) if raw_response_path else None,
            error=detail.get("error") if isinstance(detail.get("error"), dict) else None,
        )
        node.provider_calls.append(call)
        if request_id and str(request_id) not in node.request_ids:
            node.request_ids.append(str(request_id))
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

    def _save_checkpoint(self, context: JobContext) -> None:
        context.updated_at = _now_iso()
        write_json(context.job_dir / "checkpoint.json", context.model_dump(mode="json"))
        try:
            self.history_store.upsert(context)
        except Exception as exc:  # noqa: BLE001 - index is rebuildable
            if self._logger:
                self._logger.warning("execution history index update failed", error=str(exc))

    def _load_existing_context(self, job_id: str) -> tuple[JobContext, dict[str, Any]]:
        self._validate_job_id(job_id)
        raw = self.history_store.load_raw(job_id)
        self._assert_checkpoint_compatible(raw, job_id)
        try:
            context = JobContext.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - normalize checkpoint corruption
            raise ValidationError(f"Invalid H3 checkpoint for {job_id}: {exc}") from exc
        self._initialize_runtime(context)
        return context, {
            "source_master": self._source_master_path(context),
            "package": self._load_analysis_package(context, required=False),
        }

    def _assert_checkpoint_compatible(self, value: Any, job_id: str) -> None:
        if not isinstance(value, dict):
            raise ValidationError(f"Invalid checkpoint for {job_id}: root must be an object")
        if value.get("pipeline_version") != PIPELINE_VERSION:
            raise ValidationError(
                f"Checkpoint for {job_id} is not a v7 Doubao+Seedream+H3 checkpoint; old checkpoints cannot be resumed, "
                "请新建任务 (start a new job)"
            )
        if value.get("provider") not in {None, H3_PROVIDER}:
            raise ValidationError(f"Checkpoint for {job_id} belongs to another provider")
        if value.get("job_id") != job_id:
            raise ValidationError("Checkpoint job ID does not match its directory")
        job_dir = value.get("job_dir")
        if not isinstance(job_dir, str) or Path(job_dir).resolve() != (self.config.work_dir / job_id).resolve():
            raise ValidationError("Checkpoint job directory is invalid")

    def _initialize_runtime(self, context: JobContext) -> None:
        self._logger = JobLogger(
            context.job_dir / "job.log",
            callback=lambda event: self._emit_log(context, event),
        )
        self._ensure_clients()
        for client in (self.ark_client, self.minimax_client, self.uguu_client):
            if hasattr(client, "logger"):
                client.logger = self._logger

    # ---------- result, event and error helpers ----------

    def _run_and_require_preflight(self, context: JobContext, spec: JobSpec) -> None:
        self._ensure_clients()
        report = run_preflight(
            self.config,
            spec,
            job_dir=context.job_dir,
            clients={
                "ark": self.ark_client,
                "minimax_h3": self.minimax_client,
                "uguu": self.uguu_client,
            },
            logger=self._logger,
        )
        report_path = context.job_dir / "json" / "preflight.json"
        write_json(report_path, report.model_dump(mode="json"))
        self._set_artifact(context, "preflight", report_path)
        self._save_checkpoint(context)
        try:
            require_preflight(report)
        except PreflightError as exc:
            self._fail_job(context, PipelineStage.PREPARING, exc)
            raise

    def _fail_job(self, context: JobContext, stage: PipelineStage, exc: Exception) -> None:
        record = self._error_record(stage, exc).as_dict()
        context.stage = PipelineStage.FAILED
        context.progress = 0
        context.last_error = record
        self._save_checkpoint(context)
        self._emit(context, "error", record["message"], error=record)
        if self._logger:
            self._logger.error(record["message"], stage=stage.value, error_code=record.get("error_code"))

    def _handle_outer_error(self, context: JobContext, exc: Exception) -> None:
        if self._has_active_task(context):
            context.last_error = self._error_record(context.stage, exc).as_dict()
            context.stage = PipelineStage.GENERATING_SEGMENT
            self._save_checkpoint(context)
            return
        if context.stage != PipelineStage.FAILED:
            self._fail_job(context, context.stage, exc)

    @staticmethod
    def _normalize_exception(exc: Exception) -> VideoLocalizerError:
        return exc if isinstance(exc, VideoLocalizerError) else VideoLocalizerError(str(exc))

    @staticmethod
    def _error_record(stage: PipelineStage, exc: Exception) -> ErrorRecord:
        if isinstance(exc, ProviderError):
            return exc.as_record(stage.value)
        if isinstance(exc, MediaCommandError):
            return ErrorRecord(
                stage=stage.value,
                message=str(exc),
                provider="ffmpeg",
                error_code="MEDIA_COMMAND_FAILED",
                retryable=False,
            )
        return ErrorRecord(stage=stage.value, message=str(exc), error_code=exc.__class__.__name__)

    @staticmethod
    def _has_active_task(context: JobContext) -> bool:
        return any(
            attempt.status == NodeExecutionStatus.RUNNING and bool(attempt.task_id)
            for segment in context.h3_segments
            for attempt in segment.attempts
        )

    def _result(
        self,
        context: JobContext,
        *,
        action_required: str | None = None,
        segment_index: int | None = None,
        next_segment_index: int | None = None,
        message: str | None = None,
    ) -> PipelineResult:
        output = self._artifact_path(context, "final_video", required=False)
        if output is not None and not output.is_file():
            output = None
        package = self._artifact_path(context, "localization_package", required=False)
        if package is not None and not package.is_file():
            package = None
        plan = self._artifact_path(context, "doubao_h3_prompt", required=False)
        if plan is not None and not plan.is_file():
            plan = None
        return PipelineResult(
            job_id=context.job_id,
            stage=context.stage,
            output_path=output,
            package_path=package,
            plan_path=plan,
            segment_index=segment_index,
            next_segment_index=next_segment_index,
            message=message,
            action_required=action_required,
        )

    def _set_stage(self, context: JobContext, stage: PipelineStage) -> None:
        context.stage = stage
        context.progress = {
            PipelineStage.PREPARING: 5,
            PipelineStage.ANALYZING: 10,
            PipelineStage.WAITING_FOR_APPROVAL: 20,
            PipelineStage.GENERATING_REFERENCES: 25,
            PipelineStage.WAITING_FOR_REFERENCE_APPROVAL: 30,
            PipelineStage.WAITING_FOR_SEGMENTS: 10,
            PipelineStage.GENERATING_SEGMENT: self._segment_progress(
                len(context.h3_segments) or 1
            ),
            PipelineStage.WAITING_FOR_NEXT_SEGMENT: 35,
            PipelineStage.COMPLETED: 100,
            PipelineStage.FAILED: 0,
        }.get(stage, context.progress)
        self._emit(context, "stage", f"Stage: {stage.value}")
        if self._logger:
            self._logger.info("pipeline stage changed", stage=stage.value)
        self._save_checkpoint(context)

    @staticmethod
    def _segment_progress(index: int) -> int:
        return min(90, 15 + (index - 1) * 10)

    def _emit(self, context: JobContext, event_type: str, message: str, **metadata: Any) -> None:
        event = PipelineEvent(
            event_type=event_type,
            job_id=context.job_id,
            stage=context.stage,
            progress=context.progress,
            message=message,
            metadata=metadata,
        ).model_dump(mode="json")
        if self.event_callback:
            self.event_callback(event)

    def _emit_log(self, context: JobContext, log_event: dict[str, Any]) -> None:
        metadata = {
            key: value
            for key, value in log_event.items()
            if key not in {"message", "timestamp", "level"}
        }
        self._emit(context, "log", str(log_event.get("message", "")), **metadata)

    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise PipelineCancelled("Cancellation requested")

    # ---------- lookup and paths ----------

    def _source_master_path(self, context: JobContext) -> Path:
        path = self._absolute_path(context, context.source_master_artifact)
        if path is None or not path.is_file():
            path = self._artifact_path(context, "source_master", required=False)
        if path is None or not path.is_file():
            raise ValidationError("checkpoint source master artifact is missing")
        return path

    def _artifact_path(
        self,
        context: JobContext,
        name: str,
        *,
        required: bool = True,
    ) -> Path | None:
        value = context.artifacts.get(name)
        path = self._absolute_path(context, value) if value else None
        if path is None and required:
            raise ValidationError(f"checkpoint artifact is missing: {name}")
        return path

    def _set_artifact(self, context: JobContext, name: str, path: Path) -> None:
        context.artifacts[name] = self._path_reference(context, Path(path))

    @staticmethod
    def _absolute_path(context: JobContext, value: str | Path | None) -> Path | None:
        if value is None:
            return None
        path = Path(value)
        return path if path.is_absolute() else context.job_dir / path

    @staticmethod
    def _path_reference(context: JobContext, path: Path) -> str:
        path = Path(path)
        try:
            return str(path.resolve().relative_to(context.job_dir.resolve()))
        except ValueError:
            return str(path)

    @staticmethod
    def _attempt_dir(context: JobContext, segment: H3Segment, attempt: H3Attempt | int) -> Path:
        number = attempt.attempt if isinstance(attempt, H3Attempt) else int(attempt)
        path = (
            context.job_dir
            / "json"
            / "nodes"
            / "h3"
            / f"segment_{segment.index:03d}"
            / f"attempt_{number:03d}"
        )
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _doubao_attempt_dir(context: JobContext, attempt: int) -> Path:
        path = context.job_dir / "json" / "nodes" / "doubao" / f"attempt_{attempt:03d}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _seedream_attempt_dir(
        context: JobContext,
        reference: SeedreamReference,
        attempt: SeedreamAttempt | int,
    ) -> Path:
        number = attempt.attempt if isinstance(attempt, SeedreamAttempt) else int(attempt)
        path = (
            context.job_dir
            / "json"
            / "nodes"
            / "seedream"
            / f"shot_{reference.shot_id}"
            / f"attempt_{number:03d}"
        )
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _attempt_raw_dir(self, context: JobContext, segment: H3Segment, attempt: H3Attempt) -> Path:
        path = self._attempt_dir(context, segment, attempt) / "raw"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _raw_dir(self, context: JobContext) -> Path:
        path = context.job_dir / "json" / "raw"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _latest_attempt(segment: H3Segment) -> H3Attempt | None:
        return segment.attempts[-1] if segment.attempts else None

    @staticmethod
    def _latest_node(context: JobContext, node_name: str) -> NodeExecution | None:
        return next(
            (node for node in reversed(context.node_executions) if node.node == node_name),
            None,
        )

    @staticmethod
    def _active_attempt(segment: H3Segment) -> H3Attempt | None:
        if segment.active_attempt is None:
            return None
        return next(
            (
                attempt
                for attempt in segment.attempts
                if attempt.attempt == segment.active_attempt
                and attempt.status == NodeExecutionStatus.RUNNING
            ),
            None,
        )

    @staticmethod
    def _previous_segment(context: JobContext, index: int) -> H3Segment | None:
        return next((item for item in context.h3_segments if item.index == index - 1), None)

    @staticmethod
    def _reference_shots_for_range(
        context: JobContext,
        start_ms: int,
        end_ms: int,
    ) -> list[SeedreamReference]:
        return [
            reference
            for reference in context.seedream_references
            if reference.end_ms > start_ms and reference.start_ms < end_ms
        ]

    @staticmethod
    def _reference_shot_map(
        references: list[SeedreamReference],
        segment_start_ms: int,
    ) -> list[str]:
        return [
            (
                f"image {index} = shot {reference.shot_id}, source time "
                f"{max(0, reference.start_ms - segment_start_ms)}–"
                f"{max(0, reference.end_ms - segment_start_ms)} ms in this segment"
            )
            for index, reference in enumerate(references, start=1)
        ]

    def _reference_shot_map_for_segment(
        self,
        context: JobContext,
        segment: H3Segment,
    ) -> list[str]:
        references = [
            reference
            for shot_id in segment.reference_shot_ids
            for reference in context.seedream_references
            if reference.shot_id == shot_id
        ]
        return self._reference_shot_map(references, segment.source_start_ms or 0)

    @staticmethod
    def _select_segment(context: JobContext, index: int | None) -> H3Segment:
        if not context.h3_segments:
            raise ValidationError("Job has no H3 segments")
        if index is None:
            for item in reversed(context.h3_segments):
                if item.status != "completed":
                    return item
            return context.h3_segments[-1]
        for item in context.h3_segments:
            if item.index == index:
                return item
        raise ValidationError(f"H3 segment {index} was not found")

    @staticmethod
    def _latest_incomplete_segment(context: JobContext) -> H3Segment | None:
        for item in context.h3_segments:
            if item.status != "completed":
                return item
        return None

    def _node_for_attempt(
        self,
        context: JobContext,
        segment_index: int,
        attempt_number: int,
    ) -> NodeExecution | None:
        return next(
            (
                node
                for node in reversed(context.node_executions)
                if node.node == H3_NODE
                and node.segment_index == segment_index
                and node.attempt == attempt_number
            ),
            None,
        )

    @staticmethod
    def _seedream_node_for_attempt(
        context: JobContext,
        shot_id: str,
        attempt_number: int,
    ) -> NodeExecution | None:
        return next(
            (
                node
                for node in reversed(context.node_executions)
                if node.node == SEEDREAM_NODE
                and node.shot_id == shot_id
                and node.attempt == attempt_number
            ),
            None,
        )

    @staticmethod
    def _task_value(task: Any, name: str) -> Any:
        if isinstance(task, dict):
            return task.get(name)
        return getattr(task, name, None)

    def _validate_output_info(self, info: Any, *, expected_duration: float | None) -> None:
        if not info.has_video:
            raise ValidationError("H3 output has no video stream")
        if not info.has_audio:
            raise ValidationError("H3 output has no generated audio stream")
        if info.duration <= 0:
            raise ValidationError("H3 output has no positive duration")
        if (
            expected_duration is not None
            and abs(float(info.duration) - expected_duration) > H3_OUTPUT_DURATION_TOLERANCE
        ):
            raise ValidationError(
                f"H3 output duration {info.duration:.3f}s differs from requested {expected_duration:.0f}s"
            )

    def _build_cache_key(
        self,
        source: Path,
        spec: JobSpec,
        references: list[dict[str, str]],
        *,
        base_dir: Path,
    ) -> dict[str, str]:
        digest = hashlib.sha256()
        with Path(source).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        ref_digest = hashlib.sha256()
        for item in references:
            path = Path(item["path"])
            if not path.is_absolute():
                path = Path(base_dir) / path
            if path.is_file():
                ref_digest.update(path.read_bytes())
        return {
            "source_video_hash": digest.hexdigest(),
            "reference_images_hash": ref_digest.hexdigest(),
            "target_language": spec.target_language,
            "target_region": spec.target_region,
            "target_locale": spec.target_locale,
            "doubao_model": self.config.doubao_model,
            "seedream_model": self.config.seedream_model,
            "analysis_prompt_version": ANALYSIS_PROMPT_VERSION,
            "h3_model": self.config.minimax_model,
            "h3_prompt_version": H3_PROMPT_VERSION,
        }
