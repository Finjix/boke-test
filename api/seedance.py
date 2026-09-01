"""Ark Contents Generation adapter for the configured Seedance endpoint."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from api.common import ApiResponse, response_request_id
from config import AppConfig
from utils.artifacts import persist_raw_json, persist_raw_text
from utils.errors import ProviderError
from utils.ids import new_request_id
from utils.logger import JobLogger
from utils.retry import retry_call


@dataclass(frozen=True)
class SeedanceTask:
    task_id: str
    request_id: str
    raw: dict[str, Any]
    raw_path: Path | None = None


@dataclass
class SeedanceClient:
    config: AppConfig
    logger: JobLogger | None = None
    session: requests.Session | None = None
    sleeper: Any = time.sleep

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = requests.Session()

    @property
    def base_url(self) -> str:
        return self.config.ark_base_url

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.ark_api_key}",
            "Content-Type": "application/json",
        }

    def create_task(
        self,
        content: list[dict[str, Any]],
        *,
        raw_dir: Path | None = None,
        duration: int | None = None,
    ) -> SeedanceTask:
        if not self.config.seedance_model_id:
            raise ProviderError(
                "SEEDANCE_MODEL_ID is empty",
                provider="seedance",
                error_code="MODEL_ID_MISSING",
                retryable=False,
            )
        if not content:
            raise ProviderError(
                "Seedance content cannot be empty",
                provider="seedance",
                error_code="CONTENT_EMPTY",
                retryable=False,
            )
        request_id = new_request_id()
        payload: dict[str, Any] = {
            "model": self.config.seedance_model_id,
            "content": content,
            "generate_audio": True,
            "resolution": "1080p",
            "ratio": "adaptive",
            "watermark": False,
            "execution_expires_after": self.config.seedance_task_timeout,
        }
        if duration is not None:
            payload["duration"] = duration

        def operation() -> SeedanceTask:
            try:
                response = self.session.post(  # type: ignore[union-attr]
                    f"{self.base_url}/contents/generations/tasks",
                    headers=self._headers(),
                    json=payload,
                    timeout=self.config.http_timeout,
                )
            except requests.exceptions.RequestException as exc:
                raise ProviderError(
                    f"Seedance create request failed: {exc}",
                    provider="seedance",
                    request_id=request_id,
                    retryable=True,
                ) from exc

            raw_path: Path | None = None
            try:
                data = response.json()
                if raw_dir is not None:
                    raw_path = persist_raw_json(
                        raw_dir,
                        "seedance_create",
                        data,
                        request_id=request_id,
                    )
                if not isinstance(data, dict):
                    raise ProviderError(
                        "Seedance create response JSON is not an object",
                        provider="seedance",
                        status_code=getattr(response, "status_code", None),
                        request_id=request_id,
                        raw_response_path=str(raw_path) if raw_path else None,
                        error_code="INVALID_RESPONSE_OBJECT",
                        retryable=False,
                    )
            except ValueError as exc:
                body = getattr(response, "text", "")
                if raw_dir is not None:
                    raw_path = persist_raw_text(
                        raw_dir,
                        "seedance_create",
                        body,
                        request_id=request_id,
                        extension="txt",
                    )
                raise ProviderError(
                    "Seedance returned non-JSON response",
                    provider="seedance",
                    status_code=getattr(response, "status_code", None),
                    request_id=request_id,
                    raw_response_path=str(raw_path) if raw_path else None,
                ) from exc

            status = int(getattr(response, "status_code", 200))
            if status < 200 or status >= 300:
                raise ProviderError(
                    f"Seedance create HTTP {status}",
                    provider="seedance",
                    status_code=status,
                    request_id=request_id,
                    raw_response_path=str(raw_path) if raw_path else None,
                    payload=data,
                )
            task_id = data.get("id")
            if not task_id:
                raise ProviderError(
                    "Seedance create response has no id",
                    provider="seedance",
                    request_id=request_id,
                    raw_response_path=str(raw_path) if raw_path else None,
                    error_code="TASK_ID_MISSING",
                    payload=data,
                    retryable=False,
                )
            return SeedanceTask(
                task_id=str(task_id),
                request_id=response_request_id(data, request_id),
                raw=data,
                raw_path=raw_path,
            )

        return retry_call(
            operation,
            attempts=self.config.max_retries,
            on_retry=(
                lambda attempt, delay, error: self.logger.warning(
                    "retrying Seedance create request",
                    attempt=attempt,
                    delay=delay,
                    error=str(error),
                )
                if self.logger
                else None
            ),
        )

    def get_task(self, task_id: str, *, raw_dir: Path | None = None) -> ApiResponse:
        request_id = new_request_id()

        def operation() -> ApiResponse:
            try:
                response = self.session.get(  # type: ignore[union-attr]
                    f"{self.base_url}/contents/generations/tasks/{task_id}",
                    headers=self._headers(),
                    timeout=self.config.http_timeout,
                )
            except requests.exceptions.RequestException as exc:
                raise ProviderError(
                    f"Seedance query request failed: {exc}",
                    provider="seedance",
                    request_id=request_id,
                    retryable=True,
                ) from exc
            raw_path: Path | None = None
            try:
                data = response.json()
                if raw_dir is not None:
                    raw_path = persist_raw_json(
                        raw_dir,
                        "seedance_query",
                        data,
                        request_id=request_id,
                    )
                if not isinstance(data, dict):
                    raise ProviderError(
                        "Seedance query response JSON is not an object",
                        provider="seedance",
                        status_code=getattr(response, "status_code", None),
                        request_id=request_id,
                        raw_response_path=str(raw_path) if raw_path else None,
                        error_code="INVALID_RESPONSE_OBJECT",
                        retryable=False,
                    )
            except ValueError as exc:
                body = getattr(response, "text", "")
                if raw_dir is not None:
                    raw_path = persist_raw_text(
                        raw_dir,
                        "seedance_query",
                        body,
                        request_id=request_id,
                        extension="txt",
                    )
                raise ProviderError(
                    "Seedance query returned non-JSON response",
                    provider="seedance",
                    status_code=getattr(response, "status_code", None),
                    request_id=request_id,
                    raw_response_path=str(raw_path) if raw_path else None,
                ) from exc
            status = int(getattr(response, "status_code", 200))
            if status < 200 or status >= 300:
                raise ProviderError(
                    f"Seedance query HTTP {status}",
                    provider="seedance",
                    status_code=status,
                    request_id=request_id,
                    raw_response_path=str(raw_path) if raw_path else None,
                    payload=data,
                )
            return ApiResponse(
                data=data,
                request_id=response_request_id(data, request_id),
                raw_path=raw_path,
            )

        return retry_call(
            operation,
            attempts=self.config.max_retries,
            on_retry=(
                lambda attempt, delay, error: self.logger.warning(
                    "retrying Seedance query request",
                    attempt=attempt,
                    delay=delay,
                    error=str(error),
                )
                if self.logger
                else None
            ),
        )

    def wait_task(
        self,
        task_id: str,
        *,
        raw_dir: Path | None = None,
        cancel_event: Any = None,
        max_wait_seconds: float | None = None,
    ) -> ApiResponse:
        terminal_success = "succeeded"
        terminal_failure = {"failed", "cancelled", "expired"}
        wait_seconds = (
            float(self.config.seedance_task_timeout)
            if max_wait_seconds is None
            else float(max_wait_seconds)
        )
        if wait_seconds <= 0:
            raise ProviderError(
                "Seedance task wait timeout must be positive",
                provider="seedance",
                error_code="TASK_TIMEOUT",
                retryable=False,
            )
        deadline = time.monotonic() + wait_seconds
        last_response: ApiResponse | None = None
        while True:
            if cancel_event is not None and cancel_event.is_set():
                from utils.errors import PipelineCancelled

                raise PipelineCancelled("Cancellation requested while waiting for Seedance")
            if time.monotonic() >= deadline:
                raise ProviderError(
                    "Seedance task polling timed out",
                    provider="seedance",
                    error_code="TASK_TIMEOUT",
                    request_id=last_response.request_id if last_response else None,
                    raw_response_path=(
                        str(last_response.raw_path)
                        if last_response and last_response.raw_path
                        else None
                    ),
                    retryable=False,
                )
            response = self.get_task(task_id, raw_dir=raw_dir)
            last_response = response
            status = str(response.data.get("status", "")).lower()
            if status == terminal_success:
                return response
            if status in terminal_failure:
                raise ProviderError(
                    f"Seedance task ended with status {status}",
                    provider="seedance",
                    error_code=status.upper(),
                    request_id=response.request_id,
                    raw_response_path=str(response.raw_path) if response.raw_path else None,
                    payload=response.data,
                    retryable=False,
                )
            if status not in {"queued", "running"}:
                raise ProviderError(
                    f"Unknown Seedance task status: {status or '<empty>'}",
                    provider="seedance",
                    error_code="UNKNOWN_STATUS",
                    request_id=response.request_id,
                    payload=response.data,
                    retryable=False,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                continue
            self.sleeper(min(self.config.poll_interval, remaining))

    def check_access(self, *, raw_dir: Path | None = None) -> None:
        """Perform a non-generating endpoint/auth probe.

        Model-specific permission remains enforced by the create-task response;
        the configured model ID is never replaced with a guessed public ID.
        """

        if not self.config.seedance_model_id:
            raise ProviderError(
                "SEEDANCE_MODEL_ID is empty",
                provider="seedance",
                error_code="MODEL_ID_MISSING",
                retryable=False,
            )
        request_id = new_request_id()

        def operation() -> None:
            raw_path: Path | None = None
            try:
                response = self.session.get(  # type: ignore[union-attr]
                    f"{self.base_url}/contents/generations/tasks",
                    headers=self._headers(),
                    params={"limit": 1},
                    timeout=self.config.http_timeout,
                )
                status = int(getattr(response, "status_code", 200))
                try:
                    data = response.json()
                    if raw_dir is not None:
                        raw_path = persist_raw_json(
                            raw_dir,
                            "preflight_seedance",
                            data,
                            request_id=request_id,
                        )
                except ValueError as exc:
                    if raw_dir is not None:
                        raw_path = persist_raw_text(
                            raw_dir,
                            "preflight_seedance",
                            getattr(response, "text", ""),
                            request_id=request_id,
                            extension="txt",
                        )
                    raise ProviderError(
                        "Seedance endpoint probe returned non-JSON response",
                        provider="seedance",
                        status_code=status,
                        request_id=request_id,
                        raw_response_path=str(raw_path) if raw_path else None,
                        retryable=status == 429 or status >= 500,
                    ) from exc
            except requests.exceptions.RequestException as exc:
                raise ProviderError(
                    f"Seedance endpoint probe failed: {exc}",
                    provider="seedance",
                    request_id=request_id,
                    retryable=True,
                ) from exc
            if status < 200 or status >= 300:
                raise ProviderError(
                    f"Seedance endpoint probe HTTP {status}",
                    provider="seedance",
                    status_code=status,
                    request_id=request_id,
                    raw_response_path=str(raw_path) if raw_path else None,
                    payload=data,
                    retryable=status == 429 or status >= 500,
                )

        retry_call(
            operation,
            attempts=self.config.max_retries,
            on_retry=(
                lambda attempt, delay, error: self.logger.warning(
                    "retrying Seedance endpoint probe",
                    attempt=attempt,
                    delay=delay,
                    error=str(error),
                )
                if self.logger
                else None
            ),
        )
