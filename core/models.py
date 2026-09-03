"""Typed domain models shared by the UI, pipeline and adapters."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator


class PipelineStage(str, Enum):
    """Public pipeline stages shown by the desktop application."""

    PREPARING = "preparing"
    WAITING_FOR_SEGMENTS = "waiting_for_segments"
    GENERATING_SEGMENT = "generating_segment"
    WAITING_FOR_NEXT_SEGMENT = "waiting_for_next_segment"
    ANALYZING = "analyzing"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    WAITING_FOR_SEGMENT_APPROVAL = "waiting_for_segment_approval"
    GENERATING_VIDEO = "generating_video"
    COMPLETED = "completed"
    FAILED = "failed"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExecutionMode(str, Enum):
    """Compatibility value retained for callers shared with the v4 pipeline."""

    MANUAL = "manual"
    AUTO = "auto"


class ApprovalStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"


class NodeExecutionStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ProviderCall(StrictModel):
    request_id: str | None = None
    status: NodeExecutionStatus
    started_at: str
    finished_at: str | None = None
    raw_response_path: str | None = None
    error: dict[str, Any] | None = None


class NodeExecution(StrictModel):
    """One auditable provider execution."""

    node: str
    attempt: int
    status: NodeExecutionStatus
    provider: str = ""
    segment_index: StrictInt | None = None
    started_at: str
    finished_at: str | None = None
    request_ids: list[str] = Field(default_factory=list)
    task_id: str | None = None
    input_artifacts: list[str] = Field(default_factory=list)
    output_artifacts: list[str] = Field(default_factory=list)
    provider_calls: list[ProviderCall] = Field(default_factory=list)
    error: dict[str, Any] | None = None


class PipelineResult(StrictModel):
    """Result returned by each orchestration command."""

    job_id: str
    stage: PipelineStage
    output_path: Path | None = None
    package_path: Path | None = None
    plan_path: Path | None = None
    segment_index: int | None = None
    next_segment_index: int | None = None
    message: str | None = None
    action_required: str | None = None

    # Keep the completed-result ergonomics of the old ``Path`` return value
    # for integrations that only inspect the output file.
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


class HistoryEntry(StrictModel):
    """A local, provider-free summary shown in the execution history."""

    job_id: str
    job_dir: Path
    source_name: str = ""
    target_locale: str = ""
    provider: str = "doubao_seedance"
    stage: str = "unknown"
    status: str = "unknown"
    created_at: str = ""
    updated_at: str = ""
    latest_node: str | None = None
    latest_attempt: int | None = None
    last_error: str | None = None
    output_path: Path | None = None
    compatible: bool = True


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


class LocalizationPackage(StrictModel):
    """The complete planning contract passed from Doubao to Seedance."""

    source: dict[str, Any]
    target: dict[str, Any]
    video_analysis: dict[str, Any]
    speakers: list[LocalizationSpeaker]
    dialogues: list[LocalizationDialogue]
    visual_localization: dict[str, Any]
    cultural_requirements: list[str]

    @field_validator("cultural_requirements")
    @classmethod
    def non_empty_cultural_requirements(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value:
            item = item.strip()
            if not item:
                raise ValueError("cultural requirements cannot contain empty text")
            normalized.append(item)
        return normalized

    @staticmethod
    def _required_text(container: dict[str, Any], key: str, label: str) -> str:
        value = container.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label}.{key} must be non-empty text")
        return value.strip()

    @model_validator(mode="after")
    def validate_target_contract(self) -> "LocalizationPackage":
        self._required_text(self.source, "language", "source")
        self._required_text(self.target, "language", "target")
        self._required_text(self.target, "region", "target")
        self._required_text(self.target, "locale", "target")
        return self

    @model_validator(mode="after")
    def validate_speaker_references(self) -> "LocalizationPackage":
        speaker_ids = [speaker.id for speaker in self.speakers]
        if len(speaker_ids) != len(set(speaker_ids)):
            raise ValueError("speaker IDs must be unique")
        known = set(speaker_ids)
        missing = sorted({dialogue.speaker_id for dialogue in self.dialogues} - known)
        if missing:
            raise ValueError(f"dialogues reference unknown speakers: {', '.join(missing)}")
        return self

    @model_validator(mode="after")
    def validate_dialogue_timeline(self) -> "LocalizationPackage":
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

    @property
    def source_language(self) -> str:
        return str(self.source["language"])

    @property
    def target_language(self) -> str:
        return str(self.target["language"])

    @property
    def target_region(self) -> str:
        return str(self.target["region"])

    @property
    def target_locale(self) -> str:
        return str(self.target["locale"])


# Kept as a type alias for callers that used the old name; the runtime contract
# is now the package above and old checkpoints are intentionally incompatible.
LocalizationScript = LocalizationPackage


class JobSpec(StrictModel):
    input_video: Path
    target_language: str
    target_region: str
    target_locale: str
    character_refs: list[Path] = Field(default_factory=list)
    scene_refs: list[Path] = Field(default_factory=list)
    reference_images: list[Path] = Field(default_factory=list)
    transformation_instruction: str = ""
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

    @field_validator("reference_images")
    @classmethod
    def reference_image_limit(cls, value: list[Path]) -> list[Path]:
        if len(value) > 9:
            raise ValueError("at most 9 reference images may be uploaded")
        return [Path(item).expanduser() for item in value]

    @field_validator("transformation_instruction")
    @classmethod
    def normalize_transformation_instruction(cls, value: str) -> str:
        return value.strip()


class UploadedAsset(StrictModel):
    local_path: Path
    remote_url: str
    uploaded_at: str
    kind: str
    expires_at: str | None = None


class H3Attempt(StrictModel):
    """One persisted MiniMax H3 task attempt."""

    attempt: StrictInt
    status: NodeExecutionStatus
    started_at: str
    finished_at: str | None = None
    request_id: str | None = None
    task_id: str | None = None
    video_url: str | None = None
    content_artifact: str | None = None
    create_response_artifact: str | None = None
    poll_response_artifacts: list[str] = Field(default_factory=list)
    final_response_artifact: str | None = None
    failure_artifact: str | None = None
    provider_output_artifact: str | None = None
    input_artifacts: list[str] = Field(default_factory=list)
    output_artifact: str | None = None
    error: dict[str, Any] | None = None

    @field_validator("attempt")
    @classmethod
    def positive_attempt(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("H3 attempt must be positive")
        return value


class H3Segment(StrictModel):
    """A user-supplied source slice and all H3 attempts for that slice."""

    index: StrictInt
    source_duration_seconds: float
    normalized_duration_seconds: StrictInt
    prompt: str
    source_artifact: str
    status: str = "pending"
    source_asset: UploadedAsset | None = None
    reference_assets: list[UploadedAsset] = Field(default_factory=list)
    original_frame_assets: list[UploadedAsset] = Field(default_factory=list)
    previous_output_asset: UploadedAsset | None = None
    reference_strategy: str = "user_images"
    reference_video_duration_seconds: float | None = None
    attempts: list[H3Attempt] = Field(default_factory=list)
    active_attempt: StrictInt | None = None
    output_artifact: str | None = None

    @field_validator("prompt", "source_artifact", "reference_strategy")
    @classmethod
    def non_empty_segment_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("H3 segment fields cannot be empty")
        return value

    @field_validator("index", "normalized_duration_seconds")
    @classmethod
    def positive_segment_numbers(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("H3 segment numbers must be positive")
        return value

    @model_validator(mode="after")
    def validate_segment_contract(self) -> "H3Segment":
        if self.source_duration_seconds < 4 or self.source_duration_seconds > 15:
            raise ValueError("H3 source segment must be between 4 and 15 seconds")
        if not 4 <= self.normalized_duration_seconds <= 15:
            raise ValueError("H3 normalized duration must be between 4 and 15 seconds")
        if self.status not in {"pending", "running", "completed", "failed"}:
            raise ValueError(f"Unsupported H3 segment status: {self.status}")
        if (
            self.reference_video_duration_seconds is not None
            and self.reference_video_duration_seconds < 0
        ):
            raise ValueError("reference video duration cannot be negative")
        return self


class JobContext(StrictModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    pipeline_version: int = 5
    job_id: str
    job_dir: Path
    spec: JobSpec
    provider: str = "doubao_seedance"
    execution_mode: ExecutionMode = ExecutionMode.MANUAL
    approval_status: ApprovalStatus = ApprovalStatus.NOT_REQUIRED
    pending_approval: str | None = None
    approved_at: str | None = None
    created_at: str
    updated_at: str
    stage: PipelineStage = PipelineStage.ANALYZING
    progress: int = 0
    task_ids: dict[str, str] = Field(default_factory=dict)
    request_ids: dict[str, str] = Field(default_factory=dict)
    retry_counts: dict[str, int] = Field(default_factory=dict)
    node_executions: list[NodeExecution] = Field(default_factory=list)
    h3_segments: list[H3Segment] = Field(default_factory=list)
    source_master_duration_seconds: float | None = None
    source_master_artifact: str | None = None
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
