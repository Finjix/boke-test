"""MiniMax H3 adapter for the domestic asynchronous video API."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from api.common import ApiResponse, response_request_id
from config import AppConfig, FIXED_MINIMAX_H3_MODEL
from utils.artifacts import persist_raw_json, persist_raw_text
from utils.errors import PipelineCancelled, ProviderError
from utils.ids import new_request_id
from utils.logger import JobLogger
from utils.retry import retry_call


@dataclass(frozen=True)
class MiniMaxTask:
    task_id: str
    request_id: str
    raw: dict[str, Any]
    raw_path: Path | None = None


def _task_payload(data: Any) -> dict[str, Any]:
    if isinstance(data, dict) and isinstance(data.get("task"), dict):
        return data["task"]
    return data if isinstance(data, dict) else {}


def task_status(data: Any) -> str:
    return str(_task_payload(data).get("status") or "").strip().lower()


def task_video_url(data: Any) -> str | None:
    task = _task_payload(data)
    content = task.get("content")
    if isinstance(content, dict):
        for key in ("url", "video_url"):
            value = content.get(key)
            if isinstance(value, str) and value:
                return value
    for key in ("url", "video_url"):
        value = task.get(key)
        if isinstance(value, str) and value:
            return value
    return None


@dataclass
class MiniMaxClient:
    config: AppConfig
    logger: JobLogger | None = None
    session: requests.Session | None = None
    sleeper: Any = time.sleep

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = requests.Session()

    @property
    def base_url(self) -> str:
        return self.config.minimax_base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.minimax_api_key}",
            "Content-Type": "application/json",
        }

    def create_task(
        self,
        content: list[dict[str, Any]],
        *,
        duration: int,
        resolution: str | None = None,
        ratio: str = "adaptive",
        raw_dir: Path | None = None,
    ) -> MiniMaxTask:
        """Create exactly one H3 task.

        Creation requests are deliberately not retried: after a transport
        timeout it is impossible to know whether MiniMax accepted a paid task.
        The checkpoint records the unknown outcome for an explicit user retry.
        """

        if self.config.minimax_model != FIXED_MINIMAX_H3_MODEL:
            raise ProviderError(
                f"MiniMax model must be {FIXED_MINIMAX_H3_MODEL}",
                provider="minimax_h3",
                error_code="MODEL_NOT_SUPPORTED",
                retryable=False,
            )
        if not self.config.minimax_api_key:
            raise ProviderError(
                "MINIMAX_API_KEY is empty",
                provider="minimax_h3",
                error_code="API_KEY_MISSING",
                retryable=False,
            )
        if not content:
            raise ProviderError(
                "MiniMax H3 content cannot be empty",
                provider="minimax_h3",
                error_code="CONTENT_EMPTY",
                retryable=False,
            )
        if not isinstance(duration, int) or isinstance(duration, bool) or not 4 <= duration <= 15:
            raise ProviderError(
                "MiniMax H3 duration must be an integer from 4 to 15 seconds",
                provider="minimax_h3",
                error_code="INVALID_DURATION",
                retryable=False,
            )
        chosen_resolution = (resolution or self.config.minimax_resolution).upper()
        if chosen_resolution not in {"768P", "2K"}:
            raise ProviderError(
                "MiniMax H3 resolution must be 768P or 2K",
                provider="minimax_h3",
                error_code="INVALID_RESOLUTION",
                retryable=False,
            )
        request_id = new_request_id()
        payload: dict[str, Any] = {
            "model": FIXED_MINIMAX_H3_MODEL,
            "content": content,
            "duration": duration,
            "resolution": chosen_resolution,
        }
        if ratio and ratio != "adaptive":
            # In reference-video mode the input determines the aspect ratio;
            # MiniMax's official example omits ratio. Callers using pure text
            # generation may pass an explicit ratio instead.
            payload["ratio"] = ratio

        try:
            response = self.session.post(  # type: ignore[union-attr]
                f"{self.base_url}/v2/video_generation",
                headers=self._headers(),
                json=payload,
                timeout=self.config.http_timeout,
            )
        except requests.exceptions.RequestException as exc:
            raise ProviderError(
                f"MiniMax H3 create request outcome is unknown: {exc}",
                provider="minimax_h3",
                request_id=request_id,
                error_code="CREATE_OUTCOME_UNKNOWN",
                retryable=False,
            ) from exc

        raw_path: Path | None = None
        try:
            data = response.json()
        except ValueError as exc:
            if raw_dir is not None:
                raw_path = persist_raw_text(
                    raw_dir,
                    "minimax_h3_create",
                    getattr(response, "text", ""),
                    request_id=request_id,
                    extension="txt",
                )
            raise ProviderError(
                "MiniMax H3 create response is not JSON",
                provider="minimax_h3",
                status_code=int(getattr(response, "status_code", 0) or 0),
                request_id=request_id,
                raw_response_path=str(raw_path) if raw_path else None,
                error_code="INVALID_RESPONSE",
                retryable=False,
            ) from exc
        if raw_dir is not None:
            raw_path = persist_raw_json(raw_dir, "minimax_h3_create", data, request_id=request_id)
        status_code = int(getattr(response, "status_code", 200) or 200)
        if status_code < 200 or status_code >= 300:
            raise ProviderError(
                f"MiniMax H3 create HTTP {status_code}",
                provider="minimax_h3",
                status_code=status_code,
                request_id=request_id,
                raw_response_path=str(raw_path) if raw_path else None,
                payload=data,
            )
        if not isinstance(data, dict):
            raise ProviderError(
                "MiniMax H3 create response is not an object",
                provider="minimax_h3",
                request_id=request_id,
                raw_response_path=str(raw_path) if raw_path else None,
                error_code="INVALID_RESPONSE_OBJECT",
                retryable=False,
            )
        task_id = data.get("task_id") or data.get("id")
        if not task_id and isinstance(data.get("task"), dict):
            task_id = data["task"].get("task_id") or data["task"].get("id")
        if not task_id:
            raise ProviderError(
                "MiniMax H3 create response has no task_id",
                provider="minimax_h3",
                request_id=request_id,
                raw_response_path=str(raw_path) if raw_path else None,
                error_code="TASK_ID_MISSING",
                payload=data,
                retryable=False,
            )
        return MiniMaxTask(
            task_id=str(task_id),
            request_id=response_request_id(data, request_id),
            raw=data,
            raw_path=raw_path,
        )

    def get_task(self, task_id: str, *, raw_dir: Path | None = None) -> ApiResponse:
        request_id = new_request_id()

        def operation() -> ApiResponse:
            try:
                response = self.session.get(  # type: ignore[union-attr]
                    f"{self.base_url}/v2/query/video_generation/{task_id}",
                    headers=self._headers(),
                    timeout=self.config.http_timeout,
                )
            except requests.exceptions.RequestException as exc:
                raise ProviderError(
                    f"MiniMax H3 query request failed: {exc}",
                    provider="minimax_h3",
                    request_id=request_id,
                    retryable=True,
                ) from exc
            raw_path: Path | None = None
            try:
                data = response.json()
            except ValueError as exc:
                if raw_dir is not None:
                    raw_path = persist_raw_text(
                        raw_dir,
                        "minimax_h3_query",
                        getattr(response, "text", ""),
                        request_id=request_id,
                        extension="txt",
                    )
                raise ProviderError(
                    "MiniMax H3 query response is not JSON",
                    provider="minimax_h3",
                    status_code=int(getattr(response, "status_code", 0) or 0),
                    request_id=request_id,
                    raw_response_path=str(raw_path) if raw_path else None,
                    error_code="INVALID_RESPONSE",
                    retryable=False,
                ) from exc
            if raw_dir is not None:
                raw_path = persist_raw_json(raw_dir, "minimax_h3_query", data, request_id=request_id)
            status_code = int(getattr(response, "status_code", 200) or 200)
            if status_code < 200 or status_code >= 300:
                raise ProviderError(
                    f"MiniMax H3 query HTTP {status_code}",
                    provider="minimax_h3",
                    status_code=status_code,
                    request_id=request_id,
                    raw_response_path=str(raw_path) if raw_path else None,
                    payload=data,
                )
            if not isinstance(data, dict):
                raise ProviderError(
                    "MiniMax H3 query response is not an object",
                    provider="minimax_h3",
                    request_id=request_id,
                    raw_response_path=str(raw_path) if raw_path else None,
                    error_code="INVALID_RESPONSE_OBJECT",
                    retryable=False,
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
                    "retrying MiniMax H3 query request",
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
        wait_seconds = (
            float(self.config.minimax_task_timeout)
            if max_wait_seconds is None
            else float(max_wait_seconds)
        )
        if wait_seconds <= 0:
            raise ProviderError(
                "MiniMax H3 task wait timeout must be positive",
                provider="minimax_h3",
                error_code="TASK_TIMEOUT",
                retryable=False,
            )
        deadline = time.monotonic() + wait_seconds
        last: ApiResponse | None = None
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise PipelineCancelled("Cancellation requested while waiting for MiniMax H3")
            if time.monotonic() >= deadline:
                raise ProviderError(
                    "MiniMax H3 task polling timed out",
                    provider="minimax_h3",
                    error_code="TASK_TIMEOUT",
                    request_id=last.request_id if last else None,
                    raw_response_path=str(last.raw_path) if last and last.raw_path else None,
                    retryable=False,
                )
            response = self.get_task(task_id, raw_dir=raw_dir)
            last = response
            status = task_status(response.data)
            if status == "succeeded":
                return response
            if status in {"failed", "cancelled", "expired"}:
                raise ProviderError(
                    f"MiniMax H3 task ended with status {status}",
                    provider="minimax_h3",
                    error_code=status.upper(),
                    request_id=response.request_id,
                    raw_response_path=str(response.raw_path) if response.raw_path else None,
                    payload=response.data,
                    retryable=False,
                )
            if status not in {"queued", "running", "processing"}:
                raise ProviderError(
                    f"Unknown MiniMax H3 task status: {status or '<empty>'}",
                    provider="minimax_h3",
                    error_code="UNKNOWN_STATUS",
                    request_id=response.request_id,
                    raw_response_path=str(response.raw_path) if response.raw_path else None,
                    payload=response.data,
                    retryable=False,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                continue
            self.sleeper(min(self.config.poll_interval, remaining))

    def check_access(self, *, raw_dir: Path | None = None) -> None:
        """Validate local H3 configuration without creating a task."""

        del raw_dir
        if not self.config.minimax_api_key:
            raise ProviderError(
                "MINIMAX_API_KEY is empty",
                provider="minimax_h3",
                error_code="API_KEY_MISSING",
                retryable=False,
            )
        if self.config.minimax_model != FIXED_MINIMAX_H3_MODEL:
            raise ProviderError(
                f"MiniMax model must be {FIXED_MINIMAX_H3_MODEL}",
                provider="minimax_h3",
                error_code="MODEL_NOT_SUPPORTED",
                retryable=False,
            )
