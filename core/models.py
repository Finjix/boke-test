"""Typed domain models shared by the UI, pipeline and adapters."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator


class PipelineStage(str, Enum):
    """Public pipeline stages shown by the desktop application."""

    ANALYZING = "analyzing"
    GENERATING_AUDIO = "generating_audio"
    GENERATING_VIDEO = "generating_video"
    MUXING = "muxing"
    COMPLETED = "completed"
    FAILED = "failed"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LocalizationSpeaker(StrictModel):
    id: str
    visual_hint: str

    @field_validator("id", "visual_hint")
    @classmethod
    def non_empty_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("speaker fields cannot be empty")
        return value


class LocalizationDialogue(StrictModel):
    speaker_id: str
    start_ms: StrictInt
    end_ms: StrictInt
    source_text: str
    target_text: str

    @field_validator("speaker_id", "source_text", "target_text")
    @classmethod
    def non_empty_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("dialogue fields cannot be empty")
        return value

    @field_validator("start_ms", "end_ms")
    @classmethod
    def non_negative_timestamp(cls, value: int) -> int:
        if value < 0:
            raise ValueError("dialogue timestamps must be non-negative")
        return value

    @model_validator(mode="after")
    def validate_interval(self) -> "LocalizationDialogue":
        if self.end_ms <= self.start_ms:
            raise ValueError("dialogue end_ms must be greater than start_ms")
        return self


class LocalizationScript(StrictModel):
    source_language: str
    target_language: str
    speakers: list[LocalizationSpeaker]
    dialogues: list[LocalizationDialogue]

    @field_validator("source_language", "target_language")
    @classmethod
    def non_empty_language(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("languages cannot be empty")
        return value

    @model_validator(mode="after")
    def validate_speaker_references(self) -> "LocalizationScript":
        speaker_ids = [speaker.id for speaker in self.speakers]
        if len(speaker_ids) != len(set(speaker_ids)):
            raise ValueError("speaker IDs must be unique")
        known = set(speaker_ids)
        missing = sorted({dialogue.speaker_id for dialogue in self.dialogues} - known)
        if missing:
            raise ValueError(f"dialogues reference unknown speakers: {', '.join(missing)}")
        return self

    @model_validator(mode="after")
    def validate_dialogue_timeline(self) -> "LocalizationScript":
        seen: set[tuple[str, int, int, str, str]] = set()
        for index, dialogue in enumerate(self.dialogues):
            key = (
                dialogue.speaker_id,
                dialogue.start_ms,
                dialogue.end_ms,
                dialogue.source_text,
                dialogue.target_text,
            )
            if key in seen:
                raise ValueError("localization script contains duplicate dialogue")
            seen.add(key)
            if index and (
                dialogue.start_ms,
                dialogue.end_ms,
            ) < (
                self.dialogues[index - 1].start_ms,
                self.dialogues[index - 1].end_ms,
            ):
                raise ValueError("dialogues must be sorted by start_ms")
            for previous in self.dialogues[:index]:
                overlap = min(previous.end_ms, dialogue.end_ms) - max(
                    previous.start_ms,
                    dialogue.start_ms,
                )
                if overlap <= 0:
                    continue
                shorter_duration = min(
                    previous.end_ms - previous.start_ms,
                    dialogue.end_ms - dialogue.start_ms,
                )
                if overlap > shorter_duration * 0.5:
                    raise ValueError("dialogues contain an abnormally large overlap")
        return self


class JobSpec(StrictModel):
    input_video: Path
    target_language: str
    target_region: str
    target_locale: str
    character_refs: list[Path] = Field(default_factory=list)
    scene_refs: list[Path] = Field(default_factory=list)
    job_id: str | None = None

    @field_validator("target_language", "target_region", "target_locale")
    @classmethod
    def non_empty_target(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("target language, region and locale are required")
        return value

    @field_validator("target_language")
    @classmethod
    def standard_language_code(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z]{2,3}", value):
            raise ValueError("target_language must be a standard language code")
        return value.casefold()

    @field_validator("target_locale")
    @classmethod
    def bcp47_locale_code(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8})+", value):
            raise ValueError("target_locale must be a BCP-47 style locale code")
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

    pipeline_version: int = 2
    job_id: str
    job_dir: Path
    spec: JobSpec
    stage: PipelineStage = PipelineStage.ANALYZING
    progress: int = 0
    task_ids: dict[str, str] = Field(default_factory=dict)
    request_ids: dict[str, str] = Field(default_factory=dict)
    retry_counts: dict[str, int] = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)
    cache_key: dict[str, str] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    last_error: dict[str, Any] | None = None
    cancel_requested: bool = False


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


def model_to_json(value: BaseModel) -> dict[str, Any]:
    return value.model_dump(mode="json")
