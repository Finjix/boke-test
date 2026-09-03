"""Internal error types used across the application."""

from __future__ import annotations


class VideoLocalizerError(Exception):
    """Base class for errors safe to expose in the GUI."""


class ConfigurationError(VideoLocalizerError):
    pass


class ValidationError(VideoLocalizerError):
    pass


class PreflightError(VideoLocalizerError):
    def __init__(
        self,
        message: str,
        checks: list[dict[str, object]] | None = None,
    ):
        super().__init__(message)
        self.checks = checks or []


class PipelineCancelled(VideoLocalizerError):
    pass


class MediaCommandError(VideoLocalizerError):
    def __init__(self, message: str, *, command: list[str] | None = None, stderr: str = ""):
        super().__init__(message)
        self.command = command or []
        self.stderr = stderr


class ProviderError(VideoLocalizerError):
    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int | None = None,
        error_code: str | None = None,
        request_id: str | None = None,
    ):
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.error_code = error_code
        self.request_id = request_id
