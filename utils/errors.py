"""Internal error types used across the application."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class VideoLocalizerError(Exception):
    """Base class for errors safe to expose in the GUI."""


class ConfigurationError(VideoLocalizerError):
    pass


class ValidationError(VideoLocalizerError):
    pass


class PreflightError(VideoLocalizerError):
    def __init__(self, message: str, checks: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.checks = checks or []


class PipelineCancelled(VideoLocalizerError):
    pass


class MediaCommandError(VideoLocalizerError):
    def __init__(self, message: str, *, command: list[str] | None = None, stderr: str = ""):
        super().__init__(message)
        self.command = command or []
        self.stderr = stderr


@dataclass
class ErrorRecord:
    stage: str
    message: str
    provider: str | None = None
    http_status: int | None = None
    error_code: str | None = None
    raw_response_path: str | None = None
    request_id: str | None = None
    retryable: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "message": self.message,
            "provider": self.provider,
            "http_status": self.http_status,
            "error_code": self.error_code,
            "raw_response_path": self.raw_response_path,
            "request_id": self.request_id,
            "retryable": self.retryable,
        }


class ProviderError(VideoLocalizerError):
    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int | None = None,
        error_code: str | None = None,
        raw_response_path: str | None = None,
        request_id: str | None = None,
        retryable: bool | None = None,
        payload: Any = None,
    ):
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.error_code = error_code
        self.raw_response_path = raw_response_path
        self.request_id = request_id
        self.payload = payload
        self.retryable = (
            status_code == 429 or (status_code is not None and status_code >= 500)
            if retryable is None
            else retryable
        )

    def as_record(self, stage: str) -> ErrorRecord:
        return ErrorRecord(
            stage=stage,
            message=str(self),
            provider=self.provider,
            http_status=self.status_code,
            error_code=self.error_code,
            raw_response_path=self.raw_response_path,
            request_id=self.request_id,
            retryable=self.retryable,
        )
