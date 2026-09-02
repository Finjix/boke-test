"""Stateful orchestration for one v2 video localization job."""

from __future__ import annotations

import hashlib
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from api.ark import ArkClient
from api.seed_audio import SeedAudioClient
from api.seedance import SeedanceClient
from api.uguu import UguuClient
from config import AppConfig
from core.localization import ANALYSIS_PROMPT_VERSION, analyze_video
from core.models import (
    JobContext,
    JobSpec,
    LocalizationScript,
    PipelineEvent,
    PipelineStage,
    UploadedAsset,
)
from core.preflight import require_preflight, run_preflight
from core.seed_audio_prompt import (
    SEED_AUDIO_PROMPT_VERSION,
    build_seed_audio_prompt,
)
from core.seedance_prompt import SEEDANCE_PROMPT_VERSION, build_seedance_content
from media import ffmpeg
from media import ffprobe
from media.downloader import download
from utils.artifacts import read_json, write_json, write_text
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
from utils.logger import JobLogger
from video_config import validate_duration


PIPELINE_VERSION = 2
PIPELINE_STAGES: tuple[PipelineStage, ...] = (
    PipelineStage.ANALYZING,
    PipelineStage.GENERATING_AUDIO,
    PipelineStage.GENERATING_VIDEO,
    PipelineStage.MUXING,
)

_PROGRESS = {
    PipelineStage.ANALYZING: 20,
    PipelineStage.GENERATING_AUDIO: 45,
    PipelineStage.GENERATING_VIDEO: 80,
    PipelineStage.MUXING: 97,
}

_STAGE_METRIC = {
    PipelineStage.ANALYZING: "analysis_duration",
    PipelineStage.GENERATING_AUDIO: "seed_audio_duration",
    PipelineStage.GENERATING_VIDEO: "seedance_duration",
    PipelineStage.MUXING: "mux_duration",
}

_V2_ARTIFACTS = {
    "preflight",
    "source_video",
    "references",
    "source_info",
    "original_audio",
    "input_assets",
    "analysis",
    "seed_audio_prompt",
    "localized_audio",
    "assets",
    "seedance_content",
    "seedance_result",
    "localized_video",
    "final_info",
    "final_video",
}
_V2_CACHE_KEY_FIELDS = {
    "source_video_hash",
    "target_locale",
    "doubao_model",
    "seed_audio_model",
    "seedance_model",
    "analysis_prompt_version",
    "seed_audio_prompt_version",
    "seedance_prompt_version",
}
_V2_TASK_KEYS = {"seed_audio", "seedance"}


class VideoLocalizationPipeline:
    def __init__(
        self,
        config: AppConfig,
        *,
        ark_client: Any | None = None,
        seed_audio_client: Any | None = None,
        uguu_client: Any | None = None,
        seedance_client: Any | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        cancel_event: threading.Event | None = None,
    ):
        self.config = config
        self.event_callback = event_callback
        self.cancel_event = cancel_event or threading.Event()
        self._logger: JobLogger | None = None
        self.ark_client = ark_client
        self.seed_audio_client = seed_audio_client
        self.uguu_client = uguu_client
        self.seedance_client = seedance_client

    def _ensure_clients(self) -> None:
        self.ark_client = self.ark_client or ArkClient(self.config, logger=self._logger)
        self.seed_audio_client = self.seed_audio_client or SeedAudioClient(
            self.config,
            logger=self._logger,
        )
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
    ) -> Path:
        run_started = time.monotonic()
        self._ensure_clients()
        try:
            context, state = self._prepare_context(spec, job_id=job_id)
        except VideoLocalizerError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize filesystem/setup failures
            raise VideoLocalizerError(f"Failed to prepare job workspace: {exc}") from exc

        self._logger = JobLogger(
            context.job_dir / "job.log",
            callback=lambda event: self._emit_log(context, event),
        )
        self._logger.info(
            "localization job started",
            job_id=context.job_id,
            target_locale=context.spec.target_locale,
            pipeline_version=PIPELINE_VERSION,
        )
        for client in (
            self.ark_client,
            self.seed_audio_client,
            self.uguu_client,
            self.seedance_client,
        ):
            if hasattr(client, "logger"):
                client.logger = self._logger

        start_stage = self._resolve_start_stage(context, resume_from)
        if context.stage == PipelineStage.FAILED:
            context.last_error = None
            self._save_checkpoint(context)
        if not skip_preflight and start_stage == PipelineStage.ANALYZING:
            self._logger.info("running startup Preflight")
            report = run_preflight(
                self.config,
                context.spec,
                job_dir=context.job_dir,
                clients={
                    "seed_audio": self.seed_audio_client,
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

        stage_methods = {
            PipelineStage.ANALYZING: self._analyze_video,
            PipelineStage.GENERATING_AUDIO: self._generate_audio,
            PipelineStage.GENERATING_VIDEO: self._generate_video,
            PipelineStage.MUXING: self._mux_video,
        }

        try:
            start_index = PIPELINE_STAGES.index(start_stage)
            for index, stage in enumerate(PIPELINE_STAGES):
                self._check_cancel()
                if index < start_index:
                    self._logger.info("skipping completed stage", stage=stage.value)
                    continue
                self._set_stage(context, stage)
                stage_started = time.monotonic()
                stage_methods[stage](context, state)
                elapsed = round(time.monotonic() - stage_started, 3)
                context.metrics[_STAGE_METRIC[stage]] = elapsed
                self._logger.info(
                    "pipeline stage completed",
                    job_id=context.job_id,
                    target_locale=context.spec.target_locale,
                    source_video_duration=context.metrics.get("source_video_duration"),
                    stage=stage.value,
                    stage_duration_seconds=elapsed,
                )
                self._save_checkpoint(context)

            context.metrics["total_duration"] = round(time.monotonic() - run_started, 3)
            if self._logger:
                self._logger.info(
                    "localization job completed",
                    job_id=context.job_id,
                    target_locale=context.spec.target_locale,
                    source_video_duration=context.metrics.get("source_video_duration"),
                    speaker_count=context.metrics.get("speaker_count", 0),
                    dialogue_count=context.metrics.get("dialogue_count", 0),
                    total_duration_seconds=context.metrics["total_duration"],
                )
            context.stage = PipelineStage.COMPLETED
            context.progress = 100
            self._save_checkpoint(context)
            output_path = self._artifact_path(context, "final_video")
            self._emit(
                context,
                "completed",
                "Localization completed",
                output=str(output_path),
            )
            return output_path
        except PipelineCancelled as exc:
            self._fail(context, context.stage, exc, error_code="CANCELLED")
            raise
        except Exception as exc:  # noqa: BLE001 - all failures become internal records
            normalized = (
                exc if isinstance(exc, VideoLocalizerError) else VideoLocalizerError(str(exc))
            )
            self._fail(context, context.stage, normalized)
            raise normalized from exc

    def resume_failed(self, job_id: str, *, spec: JobSpec | None = None) -> Path:
        job_dir = self.config.work_dir / job_id
        checkpoint_path = job_dir / "checkpoint.json"
        if not checkpoint_path.is_file():
            raise ValidationError(f"No checkpoint found for {job_id}")
        try:
            raw = read_json(checkpoint_path)
            self._assert_checkpoint_compatible(raw, job_id)
            stored = JobContext.model_validate(raw)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - expose a project error to the GUI
            raise ValidationError(f"Invalid checkpoint for {job_id}: {exc}") from exc
        effective_spec = spec or stored.spec
        if spec is not None and spec.target_locale != stored.spec.target_locale:
            raise ValidationError("Retry spec target locale does not match the checkpoint")
        failed_stage = (stored.last_error or {}).get("stage")
        if failed_stage not in {stage.value for stage in PIPELINE_STAGES}:
            raise ValidationError("Checkpoint does not identify a resumable v2 stage")
        return self.run(
            effective_spec,
            job_id=job_id,
            resume_from=PipelineStage(failed_stage),
        )

    def _prepare_context(
        self,
        spec: JobSpec,
        *,
        job_id: str | None,
    ) -> tuple[JobContext, dict[str, Any]]:
        resolved_id = job_id or spec.job_id or new_job_id()
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
                    "Job target settings do not match the existing v2 checkpoint; start a new job"
                )
            self._assert_cache_key_current(context, spec)
            state = self._hydrate_state(context)
            return context, state

        context = JobContext(
            pipeline_version=PIPELINE_VERSION,
            job_id=resolved_id,
            job_dir=job_dir.resolve(),
            spec=spec.model_copy(update={"job_id": resolved_id}),
        )
        for relative in (
            "input",
            "input/refs/characters",
            "input/refs/scenes",
            "audio",
            "json/raw",
            "seedance",
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

        input_assets_path = self._artifact_path(context, "input_assets", required=False)
        if input_assets_path and input_assets_path.is_file():
            input_assets = [UploadedAsset.model_validate(item) for item in read_json(input_assets_path)]
            state["input_assets"] = input_assets
            for asset in input_assets:
                if asset.kind == "source_video":
                    state["source_video_asset"] = asset
                elif asset.kind == "original_audio":
                    state["original_audio_asset"] = asset

        analysis_path = self._artifact_path(context, "analysis", required=False)
        if analysis_path and analysis_path.is_file():
            state["script"] = LocalizationScript.model_validate(read_json(analysis_path))

        prompt_path = self._artifact_path(context, "seed_audio_prompt", required=False)
        if prompt_path and prompt_path.is_file():
            state["prompt"] = prompt_path.read_text(encoding="utf-8")

        for name in ("original_audio", "localized_audio", "localized_video"):
            path = self._artifact_path(context, name, required=False)
            if path and path.is_file():
                state[name] = path

        assets_path = self._artifact_path(context, "assets", required=False)
        if assets_path and assets_path.is_file():
            state["assets"] = [
                UploadedAsset.model_validate(item) for item in read_json(assets_path)
            ]

        content_path = self._artifact_path(context, "seedance_content", required=False)
        if content_path and content_path.is_file():
            state["seedance_content"] = read_json(content_path)

        result_path = self._artifact_path(context, "seedance_result", required=False)
        if result_path and result_path.is_file():
            state["seedance_result"] = read_json(result_path)
        return state

    def _assert_checkpoint_compatible(self, value: Any, job_id: str) -> None:
        if not isinstance(value, dict):
            raise ValidationError(f"Invalid checkpoint for {job_id}: root must be an object")
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
        if not isinstance(artifacts, dict) or set(artifacts) - _V2_ARTIFACTS:
            raise ValidationError(
                f"Checkpoint for {job_id} contains unsupported artifact state; 需要新建任务 (start a new job)"
            )
        cache_key = value.get("cache_key")
        if not isinstance(cache_key, dict) or not _V2_CACHE_KEY_FIELDS.issubset(cache_key):
            raise ValidationError(
                f"Checkpoint for {job_id} has no valid v2 cache key; 需要新建任务 (start a new job)"
            )
        spec = value.get("spec")
        if not isinstance(spec, dict) or set(spec) - set(JobSpec.model_fields):
            raise ValidationError(
                f"Checkpoint for {job_id} contains legacy job fields; 需要新建任务 (start a new job)"
            )
        for field in ("task_ids", "request_ids"):
            entries = value.get(field)
            if not isinstance(entries, dict) or set(entries) - _V2_TASK_KEYS:
                raise ValidationError(
                    f"Checkpoint for {job_id} contains unsupported provider state; 需要新建任务 (start a new job)"
                )

    def _assert_cache_key_current(self, context: JobContext, spec: JobSpec) -> None:
        source_copy = self._artifact_path(context, "source_video")
        expected = self._build_cache_key(source_copy, spec)
        if context.cache_key != expected:
            raise ValidationError(
                "Existing checkpoint cache key does not match the current v2 configuration; "
                "需要新建任务 (start a new job)"
            )
        if spec.input_video.is_file():
            requested_source = self._build_cache_key(spec.input_video, spec)
            if requested_source["source_video_hash"] != context.cache_key["source_video_hash"]:
                raise ValidationError(
                    "Input video does not match the existing v2 checkpoint; 需要新建任务 (start a new job)"
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
        if context.stage in PIPELINE_STAGES:
            return context.stage
        return PipelineStage.ANALYZING

    def _set_stage(self, context: JobContext, stage: PipelineStage) -> None:
        context.stage = stage
        context.progress = _PROGRESS[stage]
        self._emit(context, "stage", f"Stage: {stage.value}")
        if self._logger:
            self._logger.info("pipeline stage started", stage=stage.value)

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
        write_json(context.job_dir / "checkpoint.json", context.model_dump(mode="json"))

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
                provider="ffmpeg",
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
        if not info.has_video or not info.has_audio:
            raise ValidationError("input video must contain both video and audio streams")
        validate_duration(info.duration, self.config.seedance_max_duration)
        state["source_info"] = info.raw
        state["source_duration"] = info.duration
        context.metrics["source_video_duration"] = round(info.duration, 3)
        source_info_path = context.job_dir / "json/source_info.json"
        write_json(source_info_path, info.raw)
        self._set_artifact(context, "source_info", source_info_path)

        original_audio = context.job_dir / "audio/original_audio.wav"
        ffmpeg.extract_audio(
            source,
            original_audio,
            ffmpeg_bin=self.config.ffmpeg_bin,
            timeout=self.config.http_timeout,
        )
        if not original_audio.is_file() or original_audio.stat().st_size == 0:
            raise ValidationError("original audio extraction produced no audio file")
        state["original_audio"] = original_audio
        self._set_artifact(context, "original_audio", original_audio)

        source_asset = self.uguu_client.upload(
            source,
            kind="source_video",
            raw_dir=self._raw_dir(context),
        )
        original_audio_asset = self.uguu_client.upload(
            original_audio,
            kind="original_audio",
            raw_dir=self._raw_dir(context),
        )
        state["source_video_asset"] = source_asset
        state["original_audio_asset"] = original_audio_asset
        input_assets = [source_asset, original_audio_asset]
        state["input_assets"] = input_assets
        input_assets_path = context.job_dir / "json/input_assets.json"
        write_json(input_assets_path, [asset.model_dump(mode="json") for asset in input_assets])
        self._set_artifact(context, "input_assets", input_assets_path)

        script = analyze_video(
            self.ark_client,
            source_asset.remote_url,
            target_language=context.spec.target_language,
            target_locale=context.spec.target_locale,
            duration_seconds=info.duration,
            raw_dir=self._raw_dir(context),
            logger=self._logger,
        )
        state["script"] = script
        context.metrics["speaker_count"] = len(script.speakers)
        context.metrics["dialogue_count"] = len(script.dialogues)
        if self._logger:
            self._logger.info(
                "video analysis completed",
                job_id=context.job_id,
                target_locale=context.spec.target_locale,
                source_video_duration=round(info.duration, 3),
                speaker_count=len(script.speakers),
                dialogue_count=len(script.dialogues),
            )
        analysis_path = context.job_dir / "json/analysis.json"
        write_json(analysis_path, script.model_dump(mode="json"))
        self._set_artifact(context, "analysis", analysis_path)

    def _generate_audio(self, context: JobContext, state: dict[str, Any]) -> None:
        self._check_cancel()
        script = state.get("script")
        if not isinstance(script, LocalizationScript):
            raise ValidationError("localization analysis is missing")
        original_audio_asset = self._refresh_asset(
            context,
            state["original_audio_asset"],
        )
        state["original_audio_asset"] = original_audio_asset
        self._persist_input_assets(context, state)
        prompt = build_seed_audio_prompt(
            script,
            target_language=context.spec.target_language,
            target_locale=context.spec.target_locale,
        )
        prompt_path = context.job_dir / "json/seed_audio_prompt.txt"
        write_text(prompt_path, prompt)
        self._set_artifact(context, "seed_audio_prompt", prompt_path)

        localized_audio = context.job_dir / "audio/localized_audio.wav"
        generated = self.seed_audio_client.generate_localized_audio(
            prompt,
            original_audio_asset.remote_url,
            localized_audio,
            raw_dir=self._raw_dir(context),
        )
        self._record_task(context, "seed_audio", None, generated.request_id)
        if not localized_audio.is_file() or localized_audio.stat().st_size == 0:
            raise ProviderError(
                "Seed Audio produced no localized audio file",
                provider="seed-audio",
                request_id=generated.request_id,
                error_code="EMPTY_AUDIO_RESULT",
                retryable=False,
            )
        audio_info = ffprobe.probe(
            localized_audio,
            ffprobe_bin=self.config.ffprobe_bin,
            timeout=self.config.http_timeout,
        )
        if not audio_info.has_audio:
            raise ValidationError("localized audio output has no audio stream")
        if audio_info.duration <= 0:
            raise ValidationError("localized audio output has no positive duration")
        if "wav" not in audio_info.format_name.casefold():
            raise ValidationError("localized audio output is not a WAV file")
        state["localized_audio"] = localized_audio
        context.metrics["localized_audio_duration"] = round(audio_info.duration, 3)
        self._set_artifact(context, "localized_audio", localized_audio)

    def _generate_video(self, context: JobContext, state: dict[str, Any]) -> None:
        self._check_cancel()
        localized_audio = state.get("localized_audio")
        if not isinstance(localized_audio, Path) or not localized_audio.is_file():
            raise ValidationError("localized audio is missing")

        if "seedance" not in context.task_ids:
            source_asset = self._refresh_asset(context, state["source_video_asset"])
            state["source_video_asset"] = source_asset
            self._persist_input_assets(context, state)
            localized_audio_asset = self.uguu_client.upload(
                localized_audio,
                kind="localized_audio",
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
            assets = [
                source_asset,
                state["original_audio_asset"],
                localized_audio_asset,
                *reference_assets,
            ]
            state["assets"] = assets
            assets_path = context.job_dir / "json/assets.json"
            write_json(assets_path, [asset.model_dump(mode="json") for asset in assets])
            self._set_artifact(context, "assets", assets_path)

            references = [
                asset
                for asset in assets
                if asset.kind in {"character_reference", "scene_reference"}
            ]
            content = build_seedance_content(
                source_asset.remote_url,
                localized_audio_asset.remote_url,
                references,
                context.spec,
            )
            state["seedance_content"] = content
            content_path = context.job_dir / "json/seedance_content.json"
            write_json(content_path, content)
            self._set_artifact(context, "seedance_content", content_path)
            task = self.seedance_client.create_task(
                content,
                raw_dir=self._raw_dir(context),
            )
            self._record_task(context, "seedance", task.task_id, task.request_id)
            # Persist the task ID before polling so a failed wait can resume
            # without creating a duplicate Seedance task.
            self._save_checkpoint(context)

        if "localized_video" in state and state["localized_video"].is_file():
            return
        task_id = context.task_ids.get("seedance")
        if not task_id:
            raise ProviderError(
                "Seedance task ID is missing",
                provider="seedance",
                error_code="TASK_ID_MISSING",
                retryable=False,
            )
        response = self.seedance_client.wait_task(
            task_id,
            raw_dir=self._raw_dir(context),
            cancel_event=self.cancel_event,
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
        localized_video = context.job_dir / "seedance/localized_video.mp4"
        download(
            str(video_url),
            localized_video,
            timeout=self.config.http_timeout,
            attempts=self.config.max_retries,
        )
        state["localized_video"] = localized_video
        self._set_artifact(context, "localized_video", localized_video)
        result_path = context.job_dir / "json/seedance_result.json"
        write_json(result_path, data)
        self._set_artifact(context, "seedance_result", result_path)

    def _mux_video(self, context: JobContext, state: dict[str, Any]) -> None:
        self._check_cancel()
        localized_video = state.get("localized_video")
        localized_audio = state.get("localized_audio")
        if not isinstance(localized_video, Path) or not localized_video.is_file():
            raise ValidationError("localized video is missing")
        if not isinstance(localized_audio, Path) or not localized_audio.is_file():
            raise ValidationError("localized audio is missing")
        final_video = context.job_dir / f"output/final_{context.spec.target_locale}.mp4"
        ffmpeg.mux_video(
            localized_video,
            localized_audio,
            final_video,
            ffmpeg_bin=self.config.ffmpeg_bin,
            timeout=self.config.http_timeout,
        )
        final_info = ffprobe.probe(
            final_video,
            ffprobe_bin=self.config.ffprobe_bin,
            timeout=self.config.http_timeout,
        )
        if not final_info.has_video or not final_info.has_audio:
            raise ValidationError("final output does not contain video and audio streams")
        if final_info.duration <= 0:
            raise ValidationError("final output has no positive duration")
        metadata_path = context.job_dir / "json/final_info.json"
        write_json(metadata_path, final_info.raw)
        self._set_artifact(context, "final_info", metadata_path)
        state["final_video"] = final_video
        self._set_artifact(context, "final_video", final_video)

    def _persist_input_assets(self, context: JobContext, state: dict[str, Any]) -> None:
        assets = [
            state.get("source_video_asset"),
            state.get("original_audio_asset"),
        ]
        if any(not isinstance(asset, UploadedAsset) for asset in assets):
            raise ValidationError("checkpoint is missing source input assets")
        input_assets = [asset for asset in assets if isinstance(asset, UploadedAsset)]
        state["input_assets"] = input_assets
        path = context.job_dir / "json/input_assets.json"
        write_json(path, [asset.model_dump(mode="json") for asset in input_assets])
        self._set_artifact(context, "input_assets", path)

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
            "target_locale": spec.target_locale,
            "doubao_model": self.config.doubao_model,
            "seed_audio_model": self.config.seed_audio_model,
            "seedance_model": self.config.seedance_model_id,
            "analysis_prompt_version": ANALYSIS_PROMPT_VERSION,
            "seed_audio_prompt_version": SEED_AUDIO_PROMPT_VERSION,
            "seedance_prompt_version": SEEDANCE_PROMPT_VERSION,
        }
