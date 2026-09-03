"""Resumable MiniMax H3 direct-transformation workflow.

The workflow deliberately has no intermediate LLM.  A source video of at most
15 seconds is sent to H3 directly.  Longer videos become a local job waiting
for the user to provide ordered 4--15 second slices.  Every slice and every
paid H3 attempt is written to the job checkpoint before and after provider
calls, so a restart never silently creates a duplicate task.
"""

from __future__ import annotations

import hashlib
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from api.minimax import MiniMaxClient, task_video_url
from api.uguu import UguuClient
from config import AppConfig
from core.h3_prompt import H3_PROMPT_VERSION, build_h3_content, build_transformation_prompt
from core.models import (
    ExecutionMode,
    H3Attempt,
    H3Segment,
    JobContext,
    JobSpec,
    NodeExecution,
    NodeExecutionStatus,
    PipelineEvent,
    PipelineResult,
    PipelineStage,
    UploadedAsset,
)
from core.preflight import require_preflight, run_preflight
from media import ffprobe
from media.downloader import download
from media.ffmpeg import concat_videos, extract_uniform_frames, normalize_video
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


PIPELINE_VERSION = 5
H3_MIN_DURATION_SECONDS = 4
H3_MAX_DURATION_SECONDS = 15
H3_MAX_REFERENCE_VIDEO_SECONDS = 15.0
H3_ORIGINAL_FRAME_COUNT = 4
H3_OUTPUT_DURATION_TOLERANCE = 0.4
H3_PROVIDER = "minimax_h3"
H3_NODE = "h3"
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
        uguu_client: Any | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        cancel_event: threading.Event | None = None,
        history_store: HistoryStore | None = None,
        ffmpeg_bin: str | None = None,
    ) -> None:
        self.config = config
        self.minimax_client = minimax_client or h3_client
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
        """Create a job and run its first slice when it is at most 15 seconds."""

        del resume_from  # H3 recovery uses explicit continue/retry operations.
        try:
            mode = ExecutionMode(execution_mode)
        except ValueError as exc:
            raise ValidationError("execution_mode must be 'manual' or 'auto'") from exc

        if job_id:
            checkpoint = self.config.work_dir / job_id / "checkpoint.json"
            if checkpoint.is_file():
                context, _ = self._load_existing_context(job_id)
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

            if info.duration > H3_MAX_DURATION_SECONDS:
                self._set_stage(context, PipelineStage.WAITING_FOR_SEGMENTS)
                self._emit(
                    context,
                    "segments_required",
                    "源视频超过 15 秒，请按顺序上传 4–15 秒片段",
                    source_duration_seconds=round(info.duration, 3),
                    next_segment_index=1,
                )
                self._save_checkpoint(context)
                return self._result(
                    context,
                    action_required="append_segment",
                    next_segment_index=1,
                    message="源视频超过 15 秒，请按顺序上传每片 4–15 秒的视频",
                )

            if not skip_preflight:
                self._run_and_require_preflight(context, spec)
            segment = self._add_segment_from_path(context, master, index=1)
            return self._run_segment(context, segment)
        except PipelineCancelled as exc:
            self._handle_outer_error(context, exc)
            raise
        except Exception as exc:  # noqa: BLE001 - checkpoint every failure
            self._handle_outer_error(context, exc)
            raise self._normalize_exception(exc) from exc

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
    ) -> PipelineResult:
        """Create a fresh H3 attempt for one failed segment only."""

        context, _ = self._load_existing_context(job_id)
        segment = self._select_segment(context, segment_index)
        if segment.status == "completed":
            raise ValidationError("已完成的 H3 片段不能重试")
        active = self._active_attempt(segment)
        if active is not None and active.task_id:
            raise ValidationError("H3 task 仍在运行，请先继续等待，不能创建重复任务")
        latest = self._latest_attempt(segment)
        if latest is None or latest.status != NodeExecutionStatus.FAILED:
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
            "json/raw",
            "json/nodes/h3",
            "output",
        ):
            (job_dir / relative).mkdir(parents=True, exist_ok=True)

        source = Path(spec.input_video)
        source_copy = job_dir / "input" / f"source_master{source.suffix.lower() or '.mp4'}"
        shutil.copy2(source, source_copy)
        context.source_master_artifact = self._path_reference(context, source_copy)
        self._set_artifact(context, "source_master", source_copy)

        references: list[dict[str, str]] = []
        for index, path in enumerate(self._reference_paths(spec), start=1):
            destination = job_dir / "input" / "references" / f"reference_{index:03d}{path.suffix.lower() or '.img'}"
            shutil.copy2(path, destination)
            references.append(
                {
                    "path": self._path_reference(context, destination),
                    "kind": "reference_image",
                    "original_name": path.name,
                }
            )
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
        if len(references) > 9:
            raise ValidationError("H3 reference images cannot exceed 9 files")
        for path in references:
            if not path.is_file():
                raise ValidationError(f"Reference image does not exist: {path}")
            if path.stat().st_size > 30 * 1024 * 1024:
                raise ValidationError(f"Reference image exceeds H3 30 MiB limit: {path.name}")
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
        prompt = build_transformation_prompt(
            target_language=context.spec.target_language,
            target_region=context.spec.target_region,
            target_locale=context.spec.target_locale,
            transformation_instruction=context.spec.transformation_instruction,
            segment_index=index,
            has_previous_generated_reference=index > 1,
        )
        segment = H3Segment(
            index=index,
            source_duration_seconds=round(info.duration, 3),
            normalized_duration_seconds=normalized_duration,
            prompt=prompt,
            source_artifact=self._path_reference(context, destination),
            status="pending",
        )
        context.h3_segments.append(segment)
        context.stage = PipelineStage.GENERATING_SEGMENT
        context.progress = self._segment_progress(index)
        self._write_segments_manifest(context)
        self._save_checkpoint(context)
        return segment

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
        user_refs = self._ensure_reference_assets(context, segment)
        segment.reference_assets = user_refs

        previous_url: str | None = None
        frame_assets: list[UploadedAsset] = []
        previous = self._previous_segment(context, segment.index)
        use_previous_video = False
        if previous is not None and previous.output_artifact:
            previous_duration = float(previous.normalized_duration_seconds)
            if segment.normalized_duration_seconds + previous_duration <= H3_MAX_REFERENCE_VIDEO_SECONDS:
                previous_path = self._absolute_path(context, previous.output_artifact)
                if previous_path is None or not previous_path.is_file():
                    raise ValidationError("previous H3 output artifact is missing")
                previous_asset = self._ensure_asset(
                    context,
                    segment.previous_output_asset,
                    previous_path,
                    kind=f"h3_segment_{segment.index}_previous_output",
                )
                segment.previous_output_asset = previous_asset
                previous_url = previous_asset.remote_url
                segment.reference_video_duration_seconds = previous_duration
                segment.reference_strategy = "current_source_plus_previous_output"
                use_previous_video = True
        if previous is not None and not use_previous_video:
            if len(user_refs) + H3_ORIGINAL_FRAME_COUNT > 9:
                raise ValidationError(
                    "当前片段需要用原片四张一致性参考帧；请将用户参考图减少到 5 张以内"
                )
            master = self._source_master_path(context)
            frame_dir = context.job_dir / "input" / "references" / "original_frames"
            frame_paths = extract_uniform_frames(
                master,
                frame_dir,
                count=H3_ORIGINAL_FRAME_COUNT,
                ffprobe_bin=self.config.ffprobe_bin,
                ffmpeg_bin=self.ffmpeg_bin,
                timeout=max(self.config.http_timeout, 300.0),
            )
            frame_assets = []
            for path in frame_paths:
                asset = self._find_asset(segment.original_frame_assets, path)
                frame_assets.append(
                    self._ensure_asset(
                        context,
                        asset,
                        path,
                        kind=f"h3_segment_{segment.index}_original_frame",
                    )
                )
            segment.original_frame_assets = frame_assets
            segment.reference_strategy = "current_source_plus_original_frames"
            segment.reference_video_duration_seconds = None

        prompt = build_transformation_prompt(
            target_language=context.spec.target_language,
            target_region=context.spec.target_region,
            target_locale=context.spec.target_locale,
            transformation_instruction=context.spec.transformation_instruction,
            segment_index=segment.index,
            has_previous_generated_reference=use_previous_video,
            has_original_frame_references=bool(frame_assets),
        )
        segment.prompt = prompt
        content = build_h3_content(
            source_asset.remote_url,
            prompt,
            previous_video_url=previous_url,
            reference_assets=user_refs,
            original_frame_assets=frame_assets,
        )
        attempt_dir = self._attempt_dir(context, segment, attempt)
        content_path = attempt_dir / "content.json"
        write_json(content_path, content)
        attempt.content_artifact = self._path_reference(context, content_path)
        attempt.input_artifacts = [
            self._path_reference(context, source_path),
            *[self._path_reference(context, asset.local_path) for asset in user_refs],
            *[self._path_reference(context, asset.local_path) for asset in frame_assets],
        ]
        if previous is not None and segment.previous_output_asset is not None:
            attempt.input_artifacts.append(
                self._path_reference(context, segment.previous_output_asset.local_path)
            )
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
        if self.minimax_client is None:
            self.minimax_client = MiniMaxClient(self.config, logger=self._logger)
        if self.uguu_client is None:
            self.uguu_client = UguuClient(self.config, logger=self._logger)

    def _ensure_reference_assets(self, context: JobContext, segment: H3Segment) -> list[UploadedAsset]:
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
        return context, {"source_master": self._source_master_path(context)}

    def _assert_checkpoint_compatible(self, value: Any, job_id: str) -> None:
        if not isinstance(value, dict):
            raise ValidationError(f"Invalid checkpoint for {job_id}: root must be an object")
        if value.get("pipeline_version") != PIPELINE_VERSION:
            raise ValidationError(
                f"Checkpoint for {job_id} is not a v5 H3 checkpoint; old checkpoints cannot be resumed, "
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
        for client in (self.minimax_client, self.uguu_client):
            if hasattr(client, "logger"):
                client.logger = self._logger

    # ---------- result, event and error helpers ----------

    def _run_and_require_preflight(self, context: JobContext, spec: JobSpec) -> None:
        self._ensure_clients()
        report = run_preflight(
            self.config,
            spec,
            job_dir=context.job_dir,
            clients={"minimax_h3": self.minimax_client, "uguu": self.uguu_client},
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
        return PipelineResult(
            job_id=context.job_id,
            stage=context.stage,
            output_path=output,
            segment_index=segment_index,
            next_segment_index=next_segment_index,
            message=message,
            action_required=action_required,
        )

    def _set_stage(self, context: JobContext, stage: PipelineStage) -> None:
        context.stage = stage
        context.progress = {
            PipelineStage.PREPARING: 5,
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
            "h3_model": self.config.minimax_model,
            "h3_prompt_version": H3_PROMPT_VERSION,
        }
