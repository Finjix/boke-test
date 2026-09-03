"""Small typed models for the MiniMax two-stage video workflow."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from config import (
    MINIMAX_GENERATION_MIN_DURATION_SECONDS,
    MINIMAX_MAX_DURATION_SECONDS,
    MINIMAX_SEGMENT_MIN_DURATION_SECONDS,
)
from language_config import locale_from_code


class PipelineStage(str, Enum):
    PREPARING = "preparing"
    GENERATING_CONTEXT_IR = "generating_context_ir"
    WAITING_FOR_CONTEXT_IR = "waiting_for_context_ir"
    GENERATING_VIDEO = "generating_video"
    WAITING_FOR_VIDEO = "waiting_for_video"
    COMPLETED = "completed"
    FAILED = "failed"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PipelineResult(StrictModel):
    job_id: str
    stage: PipelineStage
    output_path: Path | None = None
    prompt_path: Path | None = None
    segment_index: int | None = None
    next_segment_index: int | None = None
    message: str | None = None

    @property
    def name(self) -> str:
        if self.output_path is None:
            raise AttributeError("pipeline result has no output path")
        return self.output_path.name

    def is_file(self) -> bool:
        return bool(self.output_path and self.output_path.is_file())

    def __fspath__(self) -> str:
        if self.output_path is None:
            raise TypeError("pipeline result has no output path")
        return str(self.output_path)


class JobSpec(StrictModel):
    input_videos: tuple[Path, ...]
    target_locale: str

    @field_validator("input_videos", mode="before")
    @classmethod
    def source_paths(cls, value: Any) -> tuple[Path, ...]:
        if isinstance(value, (str, Path)):
            value = (value,)
        if not isinstance(value, (tuple, list)):
            raise ValueError("input_videos must contain at least one video path")
        paths = tuple(Path(item).expanduser() for item in value)
        if not paths:
            raise ValueError("input_videos must contain at least one video path")
        return paths

    @field_validator("target_locale")
    @classmethod
    def valid_target_locale(cls, value: str) -> str:
        value = value.strip()
        if not value or not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8})+", value):
            raise ValueError("target_locale must be a BCP-47 style locale code")
        if locale_from_code(value) is None:
            raise ValueError(f"unsupported target locale: {value}")
        return value

    @field_validator("input_video")
    @classmethod
    def source_is_path(cls, value: Path) -> Path:
        return Path(value).expanduser()


class UploadedAsset(StrictModel):
    local_path: Path
    remote_url: str
    uploaded_at: str
    kind: str
    expires_at: str | None = None


class SegmentRun(StrictModel):
    """The in-memory and diagnostic record for one source segment."""

    index: int
    source_path: Path
    normalized_path: Path
    source_duration_seconds: float
    normalized_duration_seconds: int
    status: str = "pending"
    uploaded_url: str | None = None
    context_ir_task_id: str | None = None
    context_ir_request_id: str | None = None
    context_ir_prompt_artifact: Path | None = None
    enhanced_prompt: str | None = None
    video_task_id: str | None = None
    video_request_id: str | None = None
    provider_video_url: str | None = None
    output_path: Path | None = None
    error: str | None = None

    @field_validator("index", "normalized_duration_seconds")
    @classmethod
    def positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("segment numbers must be positive")
        return value

    @model_validator(mode="after")
    def validate_duration(self) -> "SegmentRun":
        if not MINIMAX_SEGMENT_MIN_DURATION_SECONDS <= self.source_duration_seconds <= MINIMAX_MAX_DURATION_SECONDS:
            raise ValueError("source segment must be between 3 and 15 seconds")
        if not MINIMAX_GENERATION_MIN_DURATION_SECONDS <= self.normalized_duration_seconds <= MINIMAX_MAX_DURATION_SECONDS:
            raise ValueError("normalized segment must be between 4 and 15 seconds")
        if self.status not in {"pending", "running", "completed", "failed"}:
            raise ValueError(f"unsupported segment status: {self.status}")
        return self


class ActiveJob(StrictModel):
    """Current-process state; it is intentionally not a restart checkpoint."""

    job_id: str
    job_dir: Path
    spec: JobSpec
    source_paths: list[Path] = Field(default_factory=list)
    source_master_duration_seconds: float
    stage: PipelineStage = PipelineStage.PREPARING
    progress: int = 0
    segments: list[SegmentRun] = Field(default_factory=list)
    final_output_path: Path | None = None
    current_context_ir_task_id: str | None = None
    current_video_task_id: str | None = None
    current_request_id: str | None = None
    last_prompt_path: Path | None = None
    error: str | None = None


class PipelineEvent(StrictModel):
    event_type: str
    job_id: str
    stage: PipelineStage
    progress: int = 0
    message: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class PreflightCheck(StrictModel):
    name: str
    passed: bool
    detail: str
    fatal: bool = True


class PreflightReport(StrictModel):
    passed: bool
    checks: list[PreflightCheck]
