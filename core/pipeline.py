"""Stateful orchestration for one video localization job."""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Any, Callable

from api.ark import ArkClient
from api.mediakit import MediaKitClient
from api.seed_audio import SeedAudioClient
from api.seedance import SeedanceClient
from api.uguu import UguuClient
from config import AppConfig
from core.models import (
    JobContext,
    JobSpec,
    PipelineEvent,
    PipelineStage,
    SpeakerProfile,
    UploadedAsset,
)
from core.preflight import require_preflight, run_preflight
from core.seed_audio_prompt import build_seed_audio_prompt
from core.seedance_prompt import build_seedance_content
from core.speaker import (
    build_speaker_analysis_messages,
    parse_speaker_profile,
    select_speaker_anchors,
)
from core.timeline import normalize_asr, segments_as_dicts, speaker_ids
from core.translator import translate_segments
from media import ffmpeg
from media import ffprobe
from media.downloader import download
from media.frames import extract_anchor_frames
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
from video_config import (
    TIMING_REGENERATION_ATTEMPTS,
    atempo_factor,
    duration_ratio,
    timing_is_acceptable,
    validate_duration,
)


PIPELINE_STAGES: tuple[PipelineStage, ...] = (
    PipelineStage.PROBING,
    PipelineStage.SEPARATING_AUDIO,
    PipelineStage.ASR,
    PipelineStage.ANALYZING_SPEAKERS,
    PipelineStage.TRANSLATING,
    PipelineStage.GENERATING_SEED_AUDIO,
    PipelineStage.CHECKING_AUDIO_TIMING,
    PipelineStage.UPLOADING_ASSETS,
    PipelineStage.SEEDANCE_CREATING,
    PipelineStage.SEEDANCE_RUNNING,
    PipelineStage.MIXING_AUDIO,
    PipelineStage.MUXING_VIDEO,
)

_PROGRESS = {
    PipelineStage.PROBING: 5,
    PipelineStage.SEPARATING_AUDIO: 13,
    PipelineStage.ASR: 22,
    PipelineStage.ANALYZING_SPEAKERS: 31,
    PipelineStage.TRANSLATING: 40,
    PipelineStage.GENERATING_SEED_AUDIO: 49,
    PipelineStage.CHECKING_AUDIO_TIMING: 57,
    PipelineStage.UPLOADING_ASSETS: 65,
    PipelineStage.SEEDANCE_CREATING: 72,
    PipelineStage.SEEDANCE_RUNNING: 84,
    PipelineStage.MIXING_AUDIO: 92,
    PipelineStage.MUXING_VIDEO: 97,
}


class VideoLocalizationPipeline:
    def __init__(
        self,
        config: AppConfig,
        *,
        media_client: Any | None = None,
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
        self.media_client = media_client
        self.ark_client = ark_client
        self.seed_audio_client = seed_audio_client
        self.uguu_client = uguu_client
        self.seedance_client = seedance_client

    def _ensure_clients(self) -> None:
        self.media_client = self.media_client or MediaKitClient(self.config, logger=self._logger)
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
        for client in (
            self.media_client,
            self.ark_client,
            self.seed_audio_client,
            self.uguu_client,
            self.seedance_client,
        ):
            if hasattr(client, "logger"):
                client.logger = self._logger

        start_stage = self._resolve_start_stage(context, resume_from)
        if not skip_preflight and start_stage == PipelineStage.PROBING:
            self._logger.info("running startup Preflight")
            report = run_preflight(
                self.config,
                context.spec,
                job_dir=context.job_dir,
                clients={
                    "mediakit": self.media_client,
                    "ark": self.ark_client,
                    "seed_audio": self.seed_audio_client,
                    "seedance": self.seedance_client,
                    "uguu": self.uguu_client,
                },
                logger=self._logger,
            )
            write_json(context.job_dir / "preflight.json", report.model_dump(mode="json"))
            context.artifacts["preflight"] = "preflight.json"
            self._save_checkpoint(context)
            try:
                require_preflight(report)
            except PreflightError as exc:
                self._fail(context, PipelineStage.PENDING, exc)
                raise

        stage_methods = {
            PipelineStage.PROBING: self._probe,
            PipelineStage.SEPARATING_AUDIO: self._separate_audio,
            PipelineStage.ASR: self._asr,
            PipelineStage.ANALYZING_SPEAKERS: self._analyze_speakers,
            PipelineStage.TRANSLATING: self._translate,
            PipelineStage.GENERATING_SEED_AUDIO: self._generate_seed_audio,
            PipelineStage.CHECKING_AUDIO_TIMING: self._check_audio_timing,
            PipelineStage.UPLOADING_ASSETS: self._upload_assets,
            PipelineStage.SEEDANCE_CREATING: self._create_seedance,
            PipelineStage.SEEDANCE_RUNNING: self._run_seedance,
            PipelineStage.MIXING_AUDIO: self._mix_audio,
            PipelineStage.MUXING_VIDEO: self._mux_video,
        }

        try:
            start_index = PIPELINE_STAGES.index(start_stage)
            for index, stage in enumerate(PIPELINE_STAGES):
                self._check_cancel()
                if index < start_index:
                    self._logger.info("skipping completed stage", stage=stage.value)
                    continue
                self._set_stage(context, stage)
                stage_methods[stage](context, state)
                self._save_checkpoint(context)

            context.stage = PipelineStage.SUCCEEDED
            context.progress = 100
            self._save_checkpoint(context)
            self._emit(
                context,
                "completed",
                "Localization completed",
                output=context.artifacts.get("final_video"),
            )
            return Path(context.artifacts["final_video"])
        except PipelineCancelled as exc:
            self._fail(context, context.stage, exc, error_code="CANCELLED")
            raise
        except Exception as exc:  # noqa: BLE001 - all failures become internal records
            normalized = (
                exc
                if isinstance(exc, VideoLocalizerError)
                else VideoLocalizerError(str(exc))
            )
            self._fail(context, context.stage, normalized)
            raise normalized from exc

    def resume_failed(self, job_id: str, *, spec: JobSpec | None = None) -> Path:
        job_dir = self.config.work_dir / job_id
        checkpoint_path = job_dir / "checkpoint.json"
        if not checkpoint_path.is_file():
            raise ValidationError(f"No checkpoint found for {job_id}")
        try:
            stored = JobContext.model_validate(read_json(checkpoint_path))
        except Exception as exc:  # noqa: BLE001 - expose a project error to the GUI
            raise ValidationError(f"Invalid checkpoint for {job_id}: {exc}") from exc
        effective_spec = spec or stored.spec
        failed_stage = (stored.last_error or {}).get("stage")
        if failed_stage == PipelineStage.PENDING.value:
            failed_stage = PipelineStage.PROBING.value
        if failed_stage not in {stage.value for stage in PIPELINE_STAGES}:
            raise ValidationError("Checkpoint does not identify a resumable failed stage")
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
            context = JobContext.model_validate(read_json(checkpoint_path))
            state = self._hydrate_state(context)
            return context, state

        context = JobContext(
            job_id=resolved_id,
            job_dir=job_dir.resolve(),
            spec=spec.model_copy(update={"job_id": resolved_id}),
        )
        for relative in (
            "input",
            "input/refs/characters",
            "input/refs/scenes",
            "audio",
            "frames",
            "json/raw",
            "seedance",
            "output",
        ):
            (context.job_dir / relative).mkdir(parents=True, exist_ok=True)

        if not spec.input_video.is_file():
            raise ValidationError(f"Input video does not exist: {spec.input_video}")
        source_copy = context.job_dir / "input" / spec.input_video.name
        shutil.copy2(spec.input_video, source_copy)
        context.artifacts["source_video"] = str(source_copy)

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
        write_json(context.job_dir / "input/references.json", references)
        context.artifacts["references"] = "input/references.json"
        self._save_checkpoint(context)
        return context, {"source_video": source_copy, "references": references}

    def _hydrate_state(self, context: JobContext) -> dict[str, Any]:
        state: dict[str, Any] = {
            "source_video": Path(context.artifacts["source_video"]),
        }
        references_path = context.job_dir / context.artifacts.get("references", "input/references.json")
        if references_path.is_file():
            state["references"] = read_json(references_path)
        else:
            state["references"] = {"character": [], "scene": []}
        if "source_info" in context.artifacts:
            source_info = read_json(context.job_dir / context.artifacts["source_info"])
            state["source_info"] = source_info
            duration_value = source_info.get("duration")
            if duration_value is None and isinstance(source_info.get("format"), dict):
                duration_value = source_info["format"].get("duration")
            if duration_value is None:
                raise ValidationError("checkpoint source metadata has no duration")
            state["source_duration"] = float(duration_value)
        if "voice_audio" in context.artifacts:
            state["voice_audio"] = Path(context.artifacts["voice_audio"])
        if "background_audio" in context.artifacts:
            state["background_audio"] = Path(context.artifacts["background_audio"])
        if "segments" in context.artifacts:
            from core.models import Segment

            state["segments"] = [
                Segment.model_validate(item)
                for item in read_json(context.job_dir / context.artifacts["segments"])
            ]
        if "speakers" in context.artifacts:
            state["profiles"] = [
                SpeakerProfile.model_validate(item)
                for item in read_json(context.job_dir / context.artifacts["speakers"])
            ]
        if "translated_segments" in context.artifacts:
            from core.models import Segment

            state["translated"] = [
                Segment.model_validate(item)
                for item in read_json(
                    context.job_dir / context.artifacts["translated_segments"]
                )
            ]
        if "seed_audio_prompt" in context.artifacts:
            state["prompt"] = (
                context.job_dir / context.artifacts["seed_audio_prompt"]
            ).read_text(encoding="utf-8")
        for name in ("target_voice_raw", "target_voice", "localized_video", "final_audio"):
            if name in context.artifacts:
                state[name] = Path(context.artifacts[name])
        if "assets" in context.artifacts:
            state["assets"] = [
                UploadedAsset.model_validate(item)
                for item in read_json(context.job_dir / context.artifacts["assets"])
            ]
        if "seedance_result" in context.artifacts:
            state["seedance_result"] = read_json(
                context.job_dir / context.artifacts["seedance_result"]
            )
        return state

    def _resolve_start_stage(
        self,
        context: JobContext,
        requested: PipelineStage | str | None,
    ) -> PipelineStage:
        if requested is not None:
            return PipelineStage(requested)
        if context.stage in PIPELINE_STAGES:
            return context.stage
        return PipelineStage.PROBING

    def _set_stage(self, context: JobContext, stage: PipelineStage) -> None:
        context.stage = stage
        context.progress = _PROGRESS[stage]
        self._emit(context, "stage", f"Stage: {stage.value}")
        if self._logger:
            self._logger.info("pipeline stage started", stage=stage.value)

    def _emit(self, context: JobContext, event_type: str, message: str, **metadata: Any) -> None:
        event = PipelineEvent(
            event_type=event_type,  # type: ignore[arg-type]
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
        context.artifacts[name] = str(path)

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

    def _probe(self, context: JobContext, state: dict[str, Any]) -> None:
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
        path = context.job_dir / "json/source_info.json"
        write_json(path, info.raw)
        self._set_artifact(context, "source_info", path)

    def _separate_audio(self, context: JobContext, state: dict[str, Any]) -> None:
        self._check_cancel()
        source = state["source_video"]
        result = self.media_client.separate_voice(source, raw_dir=self._raw_dir(context))
        self._record_task(context, "mediakit_separate", result.task_id, result.request_id)
        voice_url = result.result.get("voice_audio_url")
        background_url = result.result.get("background_audio_url")
        if not voice_url or not background_url:
            raise ProviderError(
                "MediaKit separation result is missing voice/background URLs",
                provider="mediakit",
                error_code="SEPARATION_RESULT_INCOMPLETE",
                retryable=False,
                payload=result.raw,
            )
        voice_path = context.job_dir / "audio/voice.wav"
        background_path = context.job_dir / "audio/background.wav"
        download(
            str(voice_url),
            voice_path,
            timeout=self.config.http_timeout,
            attempts=self.config.max_retries,
        )
        download(
            str(background_url),
            background_path,
            timeout=self.config.http_timeout,
            attempts=self.config.max_retries,
        )
        state["voice_audio"] = voice_path
        state["background_audio"] = background_path
        self._set_artifact(context, "voice_audio", voice_path)
        self._set_artifact(context, "background_audio", background_path)

    def _asr(self, context: JobContext, state: dict[str, Any]) -> None:
        self._check_cancel()
        result = self.media_client.asr(
            state["voice_audio"],
            language=context.spec.source_asr_language,
            raw_dir=self._raw_dir(context),
        )
        self._record_task(context, "mediakit_asr", result.task_id, result.request_id)
        segments = normalize_asr(result.raw)
        state["segments"] = segments
        path = context.job_dir / "json/segments.json"
        write_json(path, segments_as_dicts(segments))
        self._set_artifact(context, "segments", path)

    def _analyze_speakers(self, context: JobContext, state: dict[str, Any]) -> None:
        self._check_cancel()
        segments = state["segments"]
        ids = speaker_ids(segments)
        if len(ids) > 3:
            raise ValidationError("first release supports at most 3 speakers")
        anchors = select_speaker_anchors(segments, state["source_duration"])
        frame_paths = extract_anchor_frames(
            state["source_video"],
            anchors,
            context.job_dir / "frames",
            ffmpeg_bin=self.config.ffmpeg_bin,
        )
        frame_urls: dict[str, list[str]] = {}
        for speaker_id, paths in frame_paths.items():
            frame_urls[speaker_id] = [
                self.uguu_client.upload(
                    path,
                    kind=f"speaker_frame:{speaker_id}",
                    raw_dir=self._raw_dir(context),
                ).remote_url
                for path in paths
            ]

        profiles: list[SpeakerProfile] = []
        for speaker_id in ids:
            response = self.ark_client.chat(
                build_speaker_analysis_messages(speaker_id, frame_urls[speaker_id]),
                stage=f"speaker_analysis_{speaker_id}",
                raw_dir=self._raw_dir(context),
            )
            request_id = getattr(response, "request_id", None)
            self._record_task(context, f"speaker_analysis:{speaker_id}", None, request_id)
            content = self.ark_client.extract_text(response)
            profiles.append(parse_speaker_profile(speaker_id, content))
        state["profiles"] = profiles
        write_json(
            context.job_dir / "json/speaker_frames.json",
            frame_urls,
        )
        profiles_path = context.job_dir / "json/speakers.json"
        write_json(profiles_path, [profile.model_dump(mode="json") for profile in profiles])
        self._set_artifact(context, "speakers", profiles_path)

    def _translate(self, context: JobContext, state: dict[str, Any]) -> None:
        self._check_cancel()
        translated = translate_segments(
            self.ark_client,
            state["segments"],
            target_language=context.spec.target_language,
            target_region=context.spec.target_region,
            raw_dir=self._raw_dir(context),
            validation_attempts=self.config.max_retries,
            logger=self._logger,
        )
        self._record_task(
            context,
            "translation",
            None,
            getattr(self.ark_client, "last_request_id", None),
        )
        state["translated"] = translated
        path = context.job_dir / "json/translated_segments.json"
        write_json(path, segments_as_dicts(translated))
        self._set_artifact(context, "translated_segments", path)

    def _generate_seed_audio(self, context: JobContext, state: dict[str, Any]) -> None:
        self._check_cancel()
        prompt = build_seed_audio_prompt(
            state["translated"],
            state["profiles"],
            duration=state["source_duration"],
            target_language=context.spec.target_language,
            target_region=context.spec.target_region,
        )
        prompt_path = context.job_dir / "json/seed_audio_prompt.txt"
        write_text(prompt_path, prompt)
        self._set_artifact(context, "seed_audio_prompt", prompt_path)
        raw_path = context.job_dir / "audio/target_voice_attempt_1.wav"
        generated = self.seed_audio_client.generate_dialogue(
            prompt,
            raw_path,
            raw_dir=self._raw_dir(context),
        )
        self._record_task(context, "seed_audio", None, generated.request_id)
        state["target_voice_raw"] = raw_path
        self._set_artifact(context, "target_voice_raw", raw_path)

    def _check_audio_timing(self, context: JobContext, state: dict[str, Any]) -> None:
        self._check_cancel()
        source_duration = state["source_duration"]
        current_path = state["target_voice_raw"]
        timing_attempts: list[dict[str, Any]] = []
        for attempt in range(1, TIMING_REGENERATION_ATTEMPTS + 1):
            info = ffprobe.probe(
                current_path,
                ffprobe_bin=self.config.ffprobe_bin,
                timeout=self.config.http_timeout,
            )
            ratio = duration_ratio(info.duration, source_duration)
            timing_attempts.append(
                {
                    "attempt": attempt,
                    "path": str(current_path),
                    "generated_duration": info.duration,
                    "source_duration": source_duration,
                    "ratio": ratio,
                }
            )
            if timing_is_acceptable(ratio):
                target_path = context.job_dir / "audio/target_voice.wav"
                if abs(ratio - 1.0) <= 1e-6:
                    shutil.copy2(current_path, target_path)
                else:
                    ffmpeg.adjust_audio_tempo(
                        current_path,
                        target_path,
                        atempo_factor(info.duration, source_duration),
                        ffmpeg_bin=self.config.ffmpeg_bin,
                        timeout=self.config.http_timeout,
                    )
                state["target_voice"] = target_path
                self._set_artifact(context, "target_voice", target_path)
                write_json(context.job_dir / "json/audio_timing.json", timing_attempts)
                return
            if attempt < TIMING_REGENERATION_ATTEMPTS:
                current_path = context.job_dir / f"audio/target_voice_attempt_{attempt + 1}.wav"
                generated = self.seed_audio_client.generate_dialogue(
                    state["prompt"],
                    current_path,
                    raw_dir=self._raw_dir(context),
                )
                self._record_task(context, "seed_audio", None, generated.request_id)
        write_json(context.job_dir / "json/audio_timing.json", timing_attempts)
        raise ProviderError(
            "Seed-Audio output remained outside the 3% timing tolerance",
            provider="seed-audio",
            error_code="AUDIO_TIMING_OUT_OF_RANGE",
            retryable=False,
            payload=timing_attempts,
        )

    def _upload_assets(self, context: JobContext, state: dict[str, Any]) -> None:
        self._check_cancel()
        upload_items: list[tuple[Path, str]] = [
            (state["source_video"], "source_video"),
            (state["target_voice"], "target_voice"),
        ]
        for path in state["references"].get("character", []):
            upload_items.append((Path(path), "character_reference"))
        for path in state["references"].get("scene", []):
            upload_items.append((Path(path), "scene_reference"))
        assets = self.uguu_client.upload_many(
            upload_items,
            raw_dir=self._raw_dir(context),
        )
        state["assets"] = assets
        path = context.job_dir / "json/assets.json"
        write_json(path, [asset.model_dump(mode="json") for asset in assets])
        self._set_artifact(context, "assets", path)

    def _create_seedance(self, context: JobContext, state: dict[str, Any]) -> None:
        self._check_cancel()
        source_asset = next(asset for asset in state["assets"] if asset.kind == "source_video")
        voice_asset = next(asset for asset in state["assets"] if asset.kind == "target_voice")
        refs = [
            asset
            for asset in state["assets"]
            if asset.kind in {"character_reference", "scene_reference"}
        ]
        content = build_seedance_content(
            source_asset.remote_url,
            voice_asset.remote_url,
            refs,
            context.spec,
        )
        write_json(context.job_dir / "json/seedance_content.json", content)
        task = self.seedance_client.create_task(
            content,
            raw_dir=self._raw_dir(context),
        )
        self._record_task(context, "seedance", task.task_id, task.request_id)

    def _run_seedance(self, context: JobContext, state: dict[str, Any]) -> None:
        self._check_cancel()
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
        localized = context.job_dir / "seedance/localized_video.mp4"
        download(
            str(video_url),
            localized,
            timeout=self.config.http_timeout,
            attempts=self.config.max_retries,
        )
        state["localized_video"] = localized
        self._set_artifact(context, "localized_video", localized)
        result_path = context.job_dir / "json/seedance_result.json"
        write_json(result_path, data)
        self._set_artifact(context, "seedance_result", result_path)

    def _mix_audio(self, context: JobContext, state: dict[str, Any]) -> None:
        self._check_cancel()
        final_audio = context.job_dir / "output/final_audio.wav"
        ffmpeg.mix_audio(
            state["background_audio"],
            state["target_voice"],
            final_audio,
            ffmpeg_bin=self.config.ffmpeg_bin,
            timeout=self.config.http_timeout,
        )
        state["final_audio"] = final_audio
        self._set_artifact(context, "final_audio", final_audio)

    def _mux_video(self, context: JobContext, state: dict[str, Any]) -> None:
        self._check_cancel()
        final_video = context.job_dir / "output/final.mp4"
        ffmpeg.mux_video(
            state["localized_video"],
            state["final_audio"],
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
        self._set_artifact(context, "final_video", final_video)
