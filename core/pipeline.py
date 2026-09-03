"""In-process MiniMax H3-Context-IR -> H3 video pipeline."""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Any, Callable

from api.minimax import MiniMaxClient, task_prompt, task_video_url
from api.uguu import UguuClient
from config import (
    MINIMAX_GENERATION_MIN_DURATION_SECONDS,
    MINIMAX_MAX_DURATION_SECONDS,
    MINIMAX_SEGMENT_MIN_DURATION_SECONDS,
    AppConfig,
)
from core.h3_prompt import (
    build_context_ir_content,
    build_context_ir_prompt,
    build_video_content,
)
from core.models import ActiveJob, JobSpec, PipelineResult, PipelineStage, SegmentRun
from core.preflight import require_preflight, run_preflight
from language_config import locale_from_code
from media import ffprobe
from media.downloader import download
from media.ffmpeg import concat_videos, normalize_video
from utils.artifacts import write_json, write_text
from utils.errors import ProviderError, ValidationError
from utils.ids import new_job_id
from utils.logger import JobLogger


H3_PROVIDER = "minimax_h3"
CONTEXT_IR_PROVIDER = "minimax_context_ir"


class VideoLocalizationPipeline:
    """Run one current task; multiple selected sources are joined locally first."""

    def __init__(
        self,
        config: AppConfig,
        *,
        minimax_client: MiniMaxClient | None = None,
        uguu_client: UguuClient | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        cancel_event: threading.Event | None = None,
        ffmpeg_bin: str | None = None,
    ) -> None:
        self.config = config
        self.minimax_client = minimax_client
        self.uguu_client = uguu_client
        self.event_callback = event_callback
        self.cancel_event = cancel_event or threading.Event()
        self.ffmpeg_bin = ffmpeg_bin
        self.job: ActiveJob | None = None
        self.logger: JobLogger | None = None

    def run(self, spec: JobSpec, *, skip_preflight: bool = False) -> PipelineResult:
        if self.job is not None:
            raise ValidationError("当前已有任务，请先完成或重新启动应用")
        self._ensure_clients()
        source_paths = tuple(Path(path) for path in spec.input_videos)
        missing = [path for path in source_paths if not path.is_file()]
        if missing:
            raise ValidationError(f"source video does not exist: {missing[0]}")

        resolved_job_id = new_job_id()
        job_dir = self.config.work_dir / resolved_job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        if not skip_preflight:
            report = run_preflight(
                self.config,
                spec,
                job_dir=job_dir,
                clients={"minimax": self.minimax_client, "uguu": self.uguu_client},
                logger=self.logger,
            )
            require_preflight(report)

        infos = [
            ffprobe.probe(
                source,
                ffprobe_bin=self.config.ffprobe_bin,
                timeout=self.config.http_timeout,
            )
            for source in source_paths
        ]
        total_duration = sum(info.duration for info in infos)
        self.job = ActiveJob(
            job_id=resolved_job_id,
            job_dir=job_dir,
            spec=spec,
            source_paths=list(source_paths),
            source_master_duration_seconds=round(total_duration, 3),
        )
        self.logger = JobLogger(job_dir / "job.log", callback=self._emit_log)
        self.minimax_client.logger = self.logger
        self.uguu_client.logger = self.logger
        self._write_source_info([info.raw for info in infos])
        self._save_manifest()

        if any(not info.has_video for info in infos):
            message = "输入文件必须包含视频流"
            self._fail(message)
            raise ValidationError(message)

        invalid_input = next(
            (
                info
                for info in infos
                if not MINIMAX_SEGMENT_MIN_DURATION_SECONDS
                <= info.duration
                <= MINIMAX_MAX_DURATION_SECONDS
            ),
            None,
        )
        if invalid_input is not None:
            message = "每个视频片段需为 3–15 秒，请先切片"
            self._fail(message)
            raise ValidationError(message)
        if total_duration > MINIMAX_MAX_DURATION_SECONDS:
            message = "上传内容超过 15 秒，请先切片"
            self._fail(message)
            raise ValidationError(message)
        if total_duration < MINIMAX_SEGMENT_MIN_DURATION_SECONDS:
            message = "上传内容少于 3 秒，无法处理"
            self._fail(message)
            raise ValidationError(message)

        try:
            master = self._prepare_master(source_paths, infos)
            master_info = (
                infos[0]
                if len(source_paths) == 1
                else ffprobe.probe(
                    master,
                    ffprobe_bin=self.config.ffprobe_bin,
                    timeout=self.config.http_timeout,
                )
            )
        except Exception as exc:  # noqa: BLE001 - local media preparation is terminal
            self._fail(str(exc))
            raise

        self.job.source_master_duration_seconds = round(master_info.duration, 3)
        self._write_source_info([info.raw for info in infos], master_info.raw)
        self._save_manifest()
        if master_info.duration > MINIMAX_MAX_DURATION_SECONDS:
            message = "拼接后视频超过 15 秒，请先切片"
            self._fail(message)
            raise ValidationError(message)
        if not master_info.has_video:
            message = "拼接后视频不包含视频流"
            self._fail(message)
            raise ValidationError(message)

        output = self._process_segment(master, 1)
        self.job.final_output_path = output
        self._set_stage(PipelineStage.COMPLETED, 100)
        self._emit(
            "completed",
            "视频本地化完成",
            output=str(output),
            segment_index=1,
            source_count=len(source_paths),
        )
        return self._result(output_path=output, segment_index=1)

    def _process_segment(self, source: Path, index: int) -> Path:
        job = self._require_job()
        source = Path(source)
        if not source.is_file():
            self._fail(f"片段文件不存在：{source}")
            raise ValidationError(f"segment video does not exist: {source}")
        try:
            info = ffprobe.probe(
                source,
                ffprobe_bin=self.config.ffprobe_bin,
                timeout=self.config.http_timeout,
            )
        except Exception as exc:  # noqa: BLE001 - invalid segment is terminal
            self._fail(str(exc))
            raise
        if not info.has_video:
            self._fail("片段不包含视频流")
            raise ValidationError("segment video must contain a video stream")
        if not MINIMAX_SEGMENT_MIN_DURATION_SECONDS <= info.duration <= MINIMAX_MAX_DURATION_SECONDS:
            self._fail("片段时长必须为 3–15 秒")
            raise ValidationError("segment video must be between 3 and 15 seconds")

        segment_dir = job.job_dir / "segments" / f"segment_{index:03d}"
        input_dir = job.job_dir / "input" / "segments"
        segment_dir.mkdir(parents=True, exist_ok=True)
        input_dir.mkdir(parents=True, exist_ok=True)
        source_copy = input_dir / f"segment_{index:03d}{source.suffix.lower() or '.mp4'}"
        normalized_duration = max(
            MINIMAX_GENERATION_MIN_DURATION_SECONDS,
            min(MINIMAX_MAX_DURATION_SECONDS, int(round(info.duration))),
        )
        normalized = input_dir / f"segment_{index:03d}_normalized.mp4"
        try:
            shutil.copy2(source, source_copy)
            normalize_video(
                source_copy,
                normalized,
                duration_seconds=normalized_duration,
                source_duration_seconds=info.duration,
                ffprobe_bin=self.config.ffprobe_bin,
                ffmpeg_bin=self.ffmpeg_bin,
                timeout=self.config.http_timeout,
            )
        except Exception as exc:  # noqa: BLE001 - normalization is terminal
            self._fail(str(exc))
            raise

        segment = SegmentRun(
            index=index,
            source_path=source_copy,
            normalized_path=normalized,
            source_duration_seconds=round(info.duration, 3),
            normalized_duration_seconds=normalized_duration,
            status="running",
        )
        job.segments.append(segment)
        self._save_manifest()

        try:
            self._set_stage(PipelineStage.PREPARING, 15)
            asset = self.uguu_client.upload(
                normalized,
                kind=f"source_segment_{index}",
                raw_dir=segment_dir / "raw",
            )
            segment.uploaded_url = asset.remote_url
            requirement = build_context_ir_prompt(self._target_locale())
            ir_content = build_context_ir_content(asset.remote_url, requirement)
            write_json(segment_dir / "context_ir_content.json", ir_content)

            self._set_stage(PipelineStage.GENERATING_CONTEXT_IR, 25)
            self._emit(
                "provider_call",
                "正在请求 MiniMax H3-Context-IR",
                provider=CONTEXT_IR_PROVIDER,
                segment_index=index,
            )
            ir_task = self.minimax_client.create_context_ir_task(
                ir_content,
                duration=normalized_duration,
                ratio="adaptive",
                raw_dir=segment_dir / "raw",
            )
            segment.context_ir_task_id = ir_task.task_id
            segment.context_ir_request_id = ir_task.request_id
            job.current_context_ir_task_id = ir_task.task_id
            job.current_request_id = ir_task.request_id
            self._save_manifest()
            self._emit(
                "task",
                "MiniMax H3-Context-IR task 已创建",
                provider=CONTEXT_IR_PROVIDER,
                task_id=ir_task.task_id,
                request_id=ir_task.request_id,
                segment_index=index,
            )
            self._set_stage(PipelineStage.WAITING_FOR_CONTEXT_IR, 35)
            ir_response = self.minimax_client.wait_task(
                ir_task.task_id,
                task_kind=CONTEXT_IR_PROVIDER,
                raw_dir=segment_dir / "raw",
                cancel_event=self.cancel_event,
            )
            enhanced_prompt = task_prompt(ir_response.data)
            if not enhanced_prompt:
                raise ProviderError(
                    "MiniMax H3-Context-IR succeeded without task.content.prompt",
                    provider=CONTEXT_IR_PROVIDER,
                    request_id=ir_response.request_id,
                    raw_response_path=str(ir_response.raw_path) if ir_response.raw_path else None,
                    error_code="PROMPT_MISSING",
                    retryable=False,
                )
            prompt_path = segment_dir / "ir_prompt.txt"
            write_text(prompt_path, enhanced_prompt + "\n")
            segment.context_ir_prompt_artifact = prompt_path
            segment.enhanced_prompt = enhanced_prompt
            job.last_prompt_path = prompt_path
            job.current_context_ir_task_id = None
            self._save_manifest()
            self._emit(
                "prompt_ready",
                "MiniMax H3-Context-IR 增强提示词已生成",
                prompt=enhanced_prompt,
                prompt_path=str(prompt_path),
                segment_index=index,
            )

            h3_content = build_video_content(asset.remote_url, enhanced_prompt)
            write_json(segment_dir / "video_content.json", h3_content)
            self._set_stage(PipelineStage.GENERATING_VIDEO, 50)
            self._emit(
                "provider_call",
                "正在请求 MiniMax H3 生成视频",
                provider=H3_PROVIDER,
                segment_index=index,
            )
            h3_task = self.minimax_client.create_video_task(
                h3_content,
                duration=normalized_duration,
                resolution=self.config.minimax_resolution,
                ratio="adaptive",
                raw_dir=segment_dir / "raw",
            )
            segment.video_task_id = h3_task.task_id
            segment.video_request_id = h3_task.request_id
            job.current_video_task_id = h3_task.task_id
            job.current_request_id = h3_task.request_id
            self._save_manifest()
            self._emit(
                "task",
                "MiniMax H3 video task 已创建",
                provider=H3_PROVIDER,
                task_id=h3_task.task_id,
                request_id=h3_task.request_id,
                segment_index=index,
            )
            self._set_stage(PipelineStage.WAITING_FOR_VIDEO, 65)
            video_response = self.minimax_client.wait_task(
                h3_task.task_id,
                task_kind=H3_PROVIDER,
                raw_dir=segment_dir / "raw",
                cancel_event=self.cancel_event,
            )
            video_url = task_video_url(video_response.data)
            if not video_url:
                raise ProviderError(
                    "MiniMax H3 succeeded without task.content.url",
                    provider=H3_PROVIDER,
                    request_id=video_response.request_id,
                    raw_response_path=str(video_response.raw_path) if video_response.raw_path else None,
                    error_code="VIDEO_URL_MISSING",
                    retryable=False,
                )
            segment.provider_video_url = video_url
            output_path = job.job_dir / "output" / "segments" / f"segment_{index:03d}.mp4"
            download(video_url, output_path, timeout=self.config.http_timeout)
            output_info = ffprobe.probe(
                output_path,
                ffprobe_bin=self.config.ffprobe_bin,
                timeout=self.config.http_timeout,
            )
            if not output_info.has_video:
                raise ValidationError("MiniMax H3 output has no video stream")
            segment.output_path = output_path
            segment.status = "completed"
            job.current_video_task_id = None
            job.current_request_id = video_response.request_id
            self._save_manifest()
            return output_path
        except Exception as exc:  # noqa: BLE001 - one terminal failure per job
            segment.status = "failed"
            segment.error = str(exc)
            self._fail(str(exc))
            raise

    def _prepare_master(self, sources: tuple[Path, ...], infos: list[Any]) -> Path:
        job = self._require_job()
        source_dir = job.job_dir / "input" / "sources"
        source_dir.mkdir(parents=True, exist_ok=True)
        copies: list[Path] = []
        for index, source in enumerate(sources, start=1):
            destination = source_dir / f"source_{index:03d}{source.suffix.lower() or '.mp4'}"
            shutil.copy2(source, destination)
            copies.append(destination)
        if len(copies) == 1:
            return copies[0]
        destination = job.job_dir / "input" / "source_master.mp4"
        return concat_videos(
            copies,
            destination,
            target_duration_seconds=sum(info.duration for info in infos),
            ffprobe_bin=self.config.ffprobe_bin,
            ffmpeg_bin=self.ffmpeg_bin,
            timeout=self.config.http_timeout,
        )

    def _ensure_clients(self) -> None:
        if self.minimax_client is None:
            self.minimax_client = MiniMaxClient(self.config)
        if self.uguu_client is None:
            self.uguu_client = UguuClient(self.config)

    def _target_locale(self):
        job = self._require_job()
        locale = locale_from_code(job.spec.target_locale)
        if locale is None:
            raise ValidationError(f"unsupported target locale: {job.spec.target_locale}")
        return locale

    def _require_job(self) -> ActiveJob:
        if self.job is None:
            raise ValidationError("当前没有活动任务，请先开始处理")
        return self.job

    def _write_source_info(
        self, inputs: list[dict[str, Any]], master: dict[str, Any] | None = None
    ) -> None:
        write_json(
            self._require_job().job_dir / "json" / "source_inputs.json",
            {"inputs": inputs, "master": master},
        )

    def _save_manifest(self) -> None:
        job = self._require_job()
        write_json(job.job_dir / "json" / "session.json", job.model_dump(mode="json"))

    def _set_stage(self, stage: PipelineStage, progress: int) -> None:
        job = self._require_job()
        job.stage = stage
        job.progress = max(0, min(100, progress))
        self._save_manifest()
        self._emit("stage", f"Stage: {stage.value}")

    def _fail(self, message: str) -> None:
        if self.job is None:
            return
        self.job.stage = PipelineStage.FAILED
        self.job.progress = 0
        self.job.error = message
        self._save_manifest()
        self._emit("error", message)

    def _emit(self, event_type: str, message: str, **metadata: Any) -> None:
        if self.job is None or self.event_callback is None:
            return
        self.event_callback(
            {
                "event_type": event_type,
                "job_id": self.job.job_id,
                "stage": self.job.stage.value,
                "progress": self.job.progress,
                "message": message,
                "metadata": metadata,
            }
        )

    def _emit_log(self, event: dict[str, Any]) -> None:
        if self.event_callback is None or self.job is None:
            return
        self.event_callback(
            {
                "event_type": "log",
                "job_id": self.job.job_id,
                "stage": self.job.stage.value,
                "progress": self.job.progress,
                "message": str(event.get("message", "")),
                "metadata": event,
            }
        )

    def _result(
        self,
        *,
        output_path: Path | None = None,
        prompt_path: Path | None = None,
        segment_index: int | None = None,
        message: str | None = None,
    ) -> PipelineResult:
        job = self._require_job()
        return PipelineResult(
            job_id=job.job_id,
            stage=job.stage,
            output_path=output_path or job.final_output_path,
            prompt_path=prompt_path or job.last_prompt_path,
            segment_index=segment_index,
            message=message,
        )
