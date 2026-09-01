"""Typed domain models shared by the UI, pipeline and adapters."""

from __future__ import annotations

import math
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PipelineStage(str, Enum):
    PENDING = "PENDING"
    PROBING = "PROBING"
    SEPARATING_AUDIO = "SEPARATING_AUDIO"
    ASR = "ASR"
    ANALYZING_SPEAKERS = "ANALYZING_SPEAKERS"
    TRANSLATING = "TRANSLATING"
    GENERATING_SEED_AUDIO = "GENERATING_SEED_AUDIO"
    CHECKING_AUDIO_TIMING = "CHECKING_AUDIO_TIMING"
    UPLOADING_ASSETS = "UPLOADING_ASSETS"
    SEEDANCE_CREATING = "SEEDANCE_CREATING"
    SEEDANCE_RUNNING = "SEEDANCE_RUNNING"
    MIXING_AUDIO = "MIXING_AUDIO"
    MUXING_VIDEO = "MUXING_VIDEO"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Segment(StrictModel):
    id: str
    speaker: str
    start: float
    end: float
    duration: float | None = None
    text: str
    confidence: float | None = None

    @field_validator("id", "speaker")
    @classmethod
    def non_empty_identifier(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("segment identifiers cannot be empty")
        return value

    @field_validator("text")
    @classmethod
    def non_empty_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("segment text cannot be empty")
        return value

    @field_validator("start", "end")
    @classmethod
    def finite_timestamp(cls, value: float) -> float:
        if not math.isfinite(value) or value < 0:
            raise ValueError("timestamps must be finite and non-negative")
        return float(value)

    @field_validator("confidence")
    @classmethod
    def valid_confidence(cls, value: float | None) -> float | None:
        if value is not None and not 0 <= value <= 1:
            raise ValueError("confidence must be between 0 and 1")
        return value

    @model_validator(mode="after")
    def validate_interval(self) -> "Segment":
        if self.end <= self.start:
            raise ValueError("segment end must be greater than start")
        calculated = self.end - self.start
        if self.duration is None:
            self.duration = calculated
        elif not math.isclose(self.duration, calculated, abs_tol=1e-6):
            raise ValueError("segment duration does not match timestamps")
        return self


class SpeakerProfile(StrictModel):
    speaker_id: str
    gender: Literal["male", "female", "unknown"] = "unknown"
    age_group: Literal["child", "young", "middle", "elderly", "unknown"] = "unknown"
    role_type: Literal["main_character", "supporting", "narrator", "unknown"] = "unknown"
    voice_style: list[str] = Field(default_factory=lambda: ["neutral"])
    confidence: float | None = None

    @field_validator("speaker_id")
    @classmethod
    def non_empty_speaker_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("speaker_id cannot be empty")
        return value

    @field_validator("voice_style")
    @classmethod
    def clean_voice_style(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        return cleaned or ["neutral"]

    @field_validator("confidence")
    @classmethod
    def valid_profile_confidence(cls, value: float | None) -> float | None:
        if value is not None and not 0 <= value <= 1:
            raise ValueError("confidence must be between 0 and 1")
        return value


class JobSpec(StrictModel):
    input_video: Path
    target_language: str
    target_region: str
    character_refs: list[Path] = Field(default_factory=list)
    scene_refs: list[Path] = Field(default_factory=list)
    job_id: str | None = None

    @field_validator("target_language", "target_region")
    @classmethod
    def non_empty_target(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("target language and region are required")
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


class JobContext(StrictModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    job_id: str
    job_dir: Path
    spec: JobSpec
    stage: PipelineStage = PipelineStage.PENDING
    progress: int = 0
    task_ids: dict[str, str] = Field(default_factory=dict)
    request_ids: dict[str, str] = Field(default_factory=dict)
    retry_counts: dict[str, int] = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)
    last_error: dict[str, Any] | None = None
    cancel_requested: bool = False


class PipelineEvent(StrictModel):
    event_type: Literal["stage", "progress", "log", "task", "error", "completed"]
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


def model_to_json(value: BaseModel) -> dict[str, Any]:
    return value.model_dump(mode="json")
