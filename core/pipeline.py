"""Single-video MiniMax H3-Context-IR -> H3 pipeline."""

from __future__ import annotations

from datetime import datetime
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Callable
from uuid import uuid4

from api.minimax import MiniMaxClient, task_prompt, task_video_url
from config import (
    FIXED_MINIMAX_H3_MODEL,
    MINIMAX_SOURCE_MAX_DURATION_SECONDS,
    MINIMAX_SOURCE_MIN_DURATION_SECONDS,
    MINIMAX_VIDEO_MAX_FILE_BYTES,
    AppConfig,
)
from core.h3_prompt import (
    build_context_ir_content,
    build_context_ir_prompt,
    build_video_content,
    ensure_payload_size,
    file_data_url,
)
from core.models import (
    JobSpec,
    PipelineEvent,
    PipelineResult,
    PipelineStage,
    generation_duration,
    validate_source_duration,
)
from core.preflight import require_preflight, run_preflight
from language_config import locale_from_code
from media import ffprobe
from media.downloader import download
from media.ffmpeg import normalize_video
from utils.errors import PipelineCancelled, ProviderError, ValidationError


CONTEXT_IR_TASK_KIND = "H3-Context-IR"
VIDEO_TASK_KIND = "MiniMax-H3"


class VideoLocalizationPipeline:
    """Process exactly one source video in the current process."""

    def __init__(
        self,
        config: AppConfig,
        *,
        minimax_client: MiniMaxClient | None = None,
        event_callback: Callable[[PipelineEvent], None] | None = None,
        cancel_event: threading.Event | None = None,
        ffmpeg_bin: str | None = None,
    ) -> None:
        self.config = config
        self.minimax_client = minimax_client
        self.event_callback = event_callback
        self.cancel_event = cancel_event or threading.Event()
        self.ffmpeg_bin = ffmpeg_bin or config.ffmpeg_bin
        self._busy = False

    def run(self, spec: JobSpec, *, skip_preflight: bool = False) -> PipelineResult:
        if self._busy:
            raise ValidationError("当前已有任务")
        self._busy = True
        try:
            self._emit(PipelineStage.VALIDATING, "校验输入")
            self._ensure_clients()
            if not skip_preflight:
                require_preflight(run_preflight(self.config, spec))

            source = Path(spec.input_video)
            source_info = self._validate_source(source)
            target_duration = generation_duration(source_info.duration)
            locale = locale_from_code(spec.target_locale)
            if locale is None:
                raise ValidationError("目标地区无效")

            work_dir = Path(self.config.work_dir)
            work_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="h3-", dir=str(work_dir)) as temporary:
                temporary_dir = Path(temporary)
                normalized_source = temporary_dir / "source.mp4"
                self._check_cancel()
                normalize_video(
                    source,
                    normalized_source,
                    duration_seconds=target_duration,
                    source_duration_seconds=source_info.duration,
                    include_audio=source_info.has_audio,
                    ffprobe_bin=self.config.ffprobe_bin,
                    ffmpeg_bin=self.ffmpeg_bin,
                    timeout=self.config.http_timeout,
                )

                video_data_url = file_data_url(
                    normalized_source,
                    kind="video",
                    label="视频",
                )
                person_data_url = (
                    file_data_url(
                        Path(spec.person_image),
                        kind="image",
                        label="人物参考图",
                    )
                    if spec.person_image is not None
                    else None
                )
                scene_data_url = (
                    file_data_url(
                        Path(spec.scene_image),
                        kind="image",
                        label="场景参考图",
                    )
                    if spec.scene_image is not None
                    else None
                )

                requirement = build_context_ir_prompt(
                    locale,
                    has_person_image=person_data_url is not None,
                    has_scene_image=scene_data_url is not None,
                )
                context_content = build_context_ir_content(
                    video_data_url,
                    requirement,
                    person_image_url=person_data_url,
                    scene_image_url=scene_data_url,
                )
                ensure_payload_size(
                    {
                        "model": FIXED_MINIMAX_H3_MODEL,
                        "content": context_content,
                        "duration": target_duration,
                        "ratio": "adaptive",
                    }
                )

                self._check_cancel()
                self._emit(PipelineStage.ANALYZING, "分析视频")
                context_task = self.minimax_client.create_context_ir_task(
                    context_content,
                    duration=target_duration,
                    ratio="adaptive",
                )
                self._emit(
                    PipelineStage.WAITING_FOR_ANALYSIS,
                    "等待结构提示词",
                )
                context_response = self.minimax_client.wait_task(
                    context_task.task_id,
                    task_kind=CONTEXT_IR_TASK_KIND,
                    cancel_event=self.cancel_event,
                )
                enhanced_prompt = task_prompt(context_response.data)
                if not enhanced_prompt:
                    raise ProviderError(
                        "H3-Context-IR 未返回结构提示词",
                        provider=CONTEXT_IR_TASK_KIND,
                        error_code="PROMPT_MISSING",
                    )

                video_content = build_video_content(
                    video_data_url,
                    enhanced_prompt,
                    person_image_url=person_data_url,
                    scene_image_url=scene_data_url,
                )
                ensure_payload_size(
                    {
                        "model": FIXED_MINIMAX_H3_MODEL,
                        "content": video_content,
                        "duration": target_duration,
                        "resolution": self.config.minimax_resolution,
                        "ratio": "adaptive",
                    }
                )

                self._check_cancel()
                self._emit(PipelineStage.GENERATING, "生成视频")
                video_task = self.minimax_client.create_video_task(
                    video_content,
                    duration=target_duration,
                    resolution=self.config.minimax_resolution,
                    ratio="adaptive",
                )
                self._emit(
                    PipelineStage.WAITING_FOR_GENERATION,
                    "等待视频生成",
                )
                video_response = self.minimax_client.wait_task(
                    video_task.task_id,
                    task_kind=VIDEO_TASK_KIND,
                    cancel_event=self.cancel_event,
                )
                video_url = task_video_url(video_response.data)
                if not video_url:
                    raise ProviderError(
                        "MiniMax-H3 未返回视频地址",
                        provider=VIDEO_TASK_KIND,
                        error_code="VIDEO_URL_MISSING",
                    )

                self._check_cancel()
                self._emit(PipelineStage.DOWNLOADING, "下载结果")
                provider_output = temporary_dir / "provider_output.mp4"
                download(
                    video_url,
                    provider_output,
                    timeout=self.config.http_timeout,
                )
                result_info = ffprobe.probe(
                    provider_output,
                    ffprobe_bin=self.config.ffprobe_bin,
                    timeout=self.config.http_timeout,
                )
                if not result_info.has_video:
                    raise ValidationError("生成结果不包含视频流")

                output_path = self._publish_output(provider_output)
                self._emit(
                    PipelineStage.COMPLETED,
                    "处理完成",
                )
                return PipelineResult(
                    output_path=output_path,
                    duration_seconds=target_duration,
                )
        except Exception as exc:
            message = str(exc).strip() or "处理失败"
            self._emit(PipelineStage.FAILED, "处理失败", error=message)
            raise
        finally:
            self._busy = False

    def _ensure_clients(self) -> None:
        if self.minimax_client is None:
            self.minimax_client = MiniMaxClient(self.config)

    def _validate_source(self, source: Path):
        if not source.is_file():
            raise ValidationError("请选择存在的视频文件")
        if source.stat().st_size > MINIMAX_VIDEO_MAX_FILE_BYTES:
            raise ValidationError("视频文件不能超过 50 MB")
        try:
            info = ffprobe.probe(
                source,
                ffprobe_bin=self.config.ffprobe_bin,
                timeout=self.config.http_timeout,
            )
        except Exception as exc:
            raise ValidationError("无法读取视频信息") from exc
        if not info.has_video:
            raise ValidationError("输入文件不包含视频流")
        try:
            validate_source_duration(info.duration)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        if not (
            MINIMAX_SOURCE_MIN_DURATION_SECONDS
            <= info.duration
            <= MINIMAX_SOURCE_MAX_DURATION_SECONDS
        ):
            raise ValidationError("视频时长必须为 3–15 秒")
        return info

    def _publish_output(self, provider_output: Path) -> Path:
        output_dir = Path(self.config.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        destination = output_dir / f"{timestamp}.mp4"
        suffix = 1
        while destination.exists():
            destination = output_dir / f"{timestamp}_{suffix:02d}.mp4"
            suffix += 1
        temporary = output_dir / f".{destination.stem}.{uuid4().hex}.tmp"
        try:
            shutil.copy2(provider_output, temporary)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise PipelineCancelled("处理已取消")

    def _emit(
        self,
        stage: PipelineStage,
        message: str,
        *,
        error: str | None = None,
    ) -> None:
        if self.event_callback is not None:
            self.event_callback(
                PipelineEvent(
                    stage=stage,
                    message=message,
                    error=error,
                )
            )
