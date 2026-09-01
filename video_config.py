"""Video and audio processing limits used by the first release."""

from __future__ import annotations

from dataclasses import dataclass

from utils.errors import UnsupportedDurationError, ValidationError


AUDIO_SAMPLE_RATE = 48_000
AUDIO_TIMING_MIN_RATIO = 0.97
AUDIO_TIMING_MAX_RATIO = 1.03
MAX_SPEAKERS = 3
TIMING_REGENERATION_ATTEMPTS = 3
# This is a configurable account capability, not a public-model guarantee.
SEEDANCE_MAX_DURATION = 30
SEEDANCE_TASK_TIMEOUT = 7200


@dataclass(frozen=True)
class VideoLimits:
    seedance_max_duration: int


def duration_ratio(generated_duration: float, source_duration: float) -> float:
    if source_duration <= 0:
        raise ValidationError("source_duration must be positive")
    if generated_duration < 0:
        raise ValidationError("generated_duration must be non-negative")
    return generated_duration / source_duration


def timing_is_acceptable(ratio: float) -> bool:
    return AUDIO_TIMING_MIN_RATIO <= ratio <= AUDIO_TIMING_MAX_RATIO


def atempo_factor(generated_duration: float, source_duration: float) -> float:
    """Return the FFmpeg atempo factor that brings generated audio to source length."""

    return duration_ratio(generated_duration, source_duration)


def validate_duration(source_duration: float, max_duration: int) -> None:
    if source_duration <= 0:
        raise ValidationError("Video duration must be positive")
    if max_duration <= 0:
        raise ValidationError("Seedance maximum duration must be positive")
    if source_duration > max_duration:
        raise UnsupportedDurationError(
            f"Source duration {source_duration:.2f}s exceeds configured "
            f"Seedance limit {max_duration}s"
        )
