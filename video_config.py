"""Video-generation limits for the localization pipeline."""

from __future__ import annotations

from utils.errors import UnsupportedDurationError, ValidationError


# This is a configurable account capability, not a public-model guarantee.
SEEDANCE_MAX_DURATION = 30
SEEDANCE_TASK_TIMEOUT = 7200


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
