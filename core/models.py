"""Small domain types for the single-video workflow."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from config import (
    MINIMAX_GENERATION_MAX_DURATION_SECONDS,
    MINIMAX_GENERATION_MIN_DURATION_SECONDS,
    MINIMAX_SOURCE_MAX_DURATION_SECONDS,
    MINIMAX_SOURCE_MIN_DURATION_SECONDS,
)
from language_config import locale_from_code


class PipelineStage(str, Enum):
    VALIDATING = "validating"
    ANALYZING = "analyzing"
    WAITING_FOR_ANALYSIS = "waiting_for_analysis"
    GENERATING = "generating"
    WAITING_FOR_GENERATION = "waiting_for_generation"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class JobSpec:
    input_video: Path
    target_locale: str
    person_image: Path | None = None
    scene_image: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_video", Path(self.input_video).expanduser())
        if self.person_image is not None:
            object.__setattr__(
                self, "person_image", Path(self.person_image).expanduser()
            )
        if self.scene_image is not None:
            object.__setattr__(
                self, "scene_image", Path(self.scene_image).expanduser()
            )
        locale_code = str(self.target_locale).strip()
        if locale_from_code(locale_code) is None:
            raise ValueError(f"unsupported target locale: {locale_code}")
        object.__setattr__(self, "target_locale", locale_code)


@dataclass(frozen=True)
class PipelineResult:
    output_path: Path
    duration_seconds: int
    stage: PipelineStage = PipelineStage.COMPLETED

    @property
    def name(self) -> str:
        return self.output_path.name

    def is_file(self) -> bool:
        return self.output_path.is_file()

    def __fspath__(self) -> str:
        return str(self.output_path)


@dataclass(frozen=True)
class PipelineEvent:
    stage: PipelineStage
    message: str = ""
    error: str | None = None


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    passed: bool
    detail: str
    fatal: bool = True


@dataclass(frozen=True)
class PreflightReport:
    passed: bool
    checks: tuple[PreflightCheck, ...]


def validate_source_duration(duration: float) -> None:
    if not math.isfinite(duration) or not (
        MINIMAX_SOURCE_MIN_DURATION_SECONDS
        <= duration
        <= MINIMAX_SOURCE_MAX_DURATION_SECONDS
    ):
        raise ValueError("视频时长必须为 3–15 秒")


def generation_duration(source_duration: float) -> int:
    """Convert a valid source duration to the integer accepted by H3."""

    rounded_up = math.ceil(source_duration - 1e-9)
    return max(
        MINIMAX_GENERATION_MIN_DURATION_SECONDS,
        min(MINIMAX_GENERATION_MAX_DURATION_SECONDS, rounded_up),
    )
