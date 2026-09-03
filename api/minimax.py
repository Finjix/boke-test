"""MiniMax H3-Context-IR and H3 video API adapter."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from api.common import ApiResponse, response_request_id
from config import (
    FIXED_MINIMAX_H3_MODEL,
    MINIMAX_GENERATION_MIN_DURATION_SECONDS,
    MINIMAX_H3_RESOLUTIONS,
    MINIMAX_MAX_DURATION_SECONDS,
    AppConfig,
)
from utils.artifacts import persist_raw_json, persist_raw_text
from utils.errors import PipelineCancelled, ProviderError, ValidationError
from utils.ids import new_request_id
from utils.logger import JobLogger


@dataclass(frozen=True)
class MiniMaxTask:
    task_id: str
    request_id: str
    raw: dict[str, Any]
    task_kind: str
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
        value = content.get("url")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def task_prompt(data: Any) -> str | None:
    task = _task_payload(data)
    content = task.get("content")
    if isinstance(content, dict):
        value = content.get("prompt")
        if isinstance(value, str) and value.strip():
            return value
    return None


class MiniMaxClient:
    def __init__(
        self,
        config: AppConfig,
        *,
        logger: JobLogger | None = None,
        session: requests.Session | None = None,
        sleeper: Any = time.sleep,
    ) -> None:
        self.config = config
        self.logger = logger
        self.sleeper = sleeper
        self.session = session or requests.Session()
        # The domestic endpoint is directly reachable on this host. Custom
        # endpoints retain requests' normal proxy behavior.
        if urlparse(self.base_url).hostname == "api.minimax.cn":
            self.session.trust_env = False

    @property
    def base_url(self) -> str:
        return self.config.minimax_base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.minimax_api_key}",
            "Content-Type": "application/json",
        }

    def _validate_common(self, content: list[dict[str, Any]], duration: int) -> None:
        if self.config.minimax_model != FIXED_MINIMAX_H3_MODEL:
            raise ProviderError(
                f"MiniMax model must be {FIXED_MINIMAX_H3_MODEL}",
                provider="minimax",
                error_code="MODEL_NOT_SUPPORTED",
                retryable=False,
            )
        if not self.config.minimax_api_key:
            raise ProviderError(
                "MINIMAX_API_KEY is empty",
                provider="minimax",
                error_code="API_KEY_MISSING",
                retryable=False,
            )
        if not isinstance(content, list) or not content:
            raise ProviderError(
                "MiniMax content cannot be empty",
                provider="minimax",
                error_code="CONTENT_EMPTY",
                retryable=False,
            )
        if not any(
            isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
            and item["text"].strip()
            for item in content
        ):
            raise ProviderError(
                "MiniMax content must include a non-empty text item",
                provider="minimax",
                error_code="PROMPT_EMPTY",
                retryable=False,
            )
        if not isinstance(duration, int) or isinstance(duration, bool):
            raise ProviderError(
                "MiniMax duration must be an integer",
                provider="minimax",
                error_code="INVALID_DURATION",
                retryable=False,
            )
        if not MINIMAX_GENERATION_MIN_DURATION_SECONDS <= duration <= MINIMAX_MAX_DURATION_SECONDS:
            raise ProviderError(
                "MiniMax duration must be an integer from 4 to 15 seconds",
                provider="minimax",
                error_code="INVALID_DURATION",
                retryable=False,
            )

    def _create_task(
        self,
        *,
        endpoint: str,
        payload: dict[str, Any],
        task_kind: str,
        raw_stage: str,
        raw_dir: Path | None,
    ) -> MiniMaxTask:
        request_id = new_request_id()
        try:
            response = self.session.post(
                f"{self.base_url}{endpoint}",
                headers=self._headers(),
                json=payload,
                timeout=self.config.http_timeout,
            )
        except requests.exceptions.RequestException as exc:
            raise ProviderError(
                f"MiniMax {task_kind} create request failed: {exc}",
                provider=task_kind,
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
                    raw_stage,
                    getattr(response, "text", ""),
                    request_id=request_id,
                    extension="txt",
                )
            raise ProviderError(
                f"MiniMax {task_kind} create response is not JSON",
                provider=task_kind,
                status_code=int(getattr(response, "status_code", 0) or 0),
                request_id=request_id,
                raw_response_path=str(raw_path) if raw_path else None,
                error_code="INVALID_RESPONSE",
                retryable=False,
            ) from exc
        if raw_dir is not None:
            raw_path = persist_raw_json(raw_dir, raw_stage, data, request_id=request_id)
        status_code = int(getattr(response, "status_code", 200) or 200)
        if status_code < 200 or status_code >= 300:
            raise ProviderError(
                f"MiniMax {task_kind} create HTTP {status_code}",
                provider=task_kind,
                status_code=status_code,
                request_id=request_id,
                raw_response_path=str(raw_path) if raw_path else None,
                payload=data,
                retryable=False,
            )
        if not isinstance(data, dict):
            raise ProviderError(
                f"MiniMax {task_kind} create response is not an object",
                provider=task_kind,
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
                f"MiniMax {task_kind} create response has no task_id",
                provider=task_kind,
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
            task_kind=task_kind,
            raw_path=raw_path,
        )

    def create_context_ir_task(
        self,
        content: list[dict[str, Any]],
        *,
        duration: int,
        ratio: str = "adaptive",
        raw_dir: Path | None = None,
    ) -> MiniMaxTask:
        self._validate_common(content, duration)
        if not ratio:
            raise ValidationError("MiniMax Context-IR ratio cannot be empty")
        return self._create_task(
            endpoint="/v2/h3_context_ir",
            payload={
                "model": FIXED_MINIMAX_H3_MODEL,
                "content": content,
                "duration": duration,
                "ratio": ratio,
            },
            task_kind="minimax_context_ir",
            raw_stage="minimax_context_ir_create",
            raw_dir=raw_dir,
        )

    def create_video_task(
        self,
        content: list[dict[str, Any]],
        *,
        duration: int,
        resolution: str | None = None,
        ratio: str = "adaptive",
        raw_dir: Path | None = None,
    ) -> MiniMaxTask:
        self._validate_common(content, duration)
        chosen_resolution = (resolution or self.config.minimax_resolution).upper()
        if chosen_resolution not in MINIMAX_H3_RESOLUTIONS:
            raise ProviderError(
                "MiniMax H3 resolution must be 768P or 2K",
                provider="minimax_h3",
                error_code="INVALID_RESOLUTION",
                retryable=False,
            )
        if not ratio:
            raise ValidationError("MiniMax H3 ratio cannot be empty")
        return self._create_task(
            endpoint="/v2/video_generation",
            payload={
                "model": FIXED_MINIMAX_H3_MODEL,
                "content": content,
                "duration": duration,
                "resolution": chosen_resolution,
                "ratio": ratio,
            },
            task_kind="minimax_h3",
            raw_stage="minimax_h3_create",
            raw_dir=raw_dir,
        )

    def get_task(
        self,
        task_id: str,
        *,
        task_kind: str = "minimax",
        raw_dir: Path | None = None,
    ) -> ApiResponse:
        request_id = new_request_id()
        try:
            response = self.session.get(
                f"{self.base_url}/v2/query/video_generation/{task_id}",
                headers=self._headers(),
                timeout=self.config.http_timeout,
            )
        except requests.exceptions.RequestException as exc:
            raise ProviderError(
                f"MiniMax {task_kind} query request failed: {exc}",
                provider=task_kind,
                request_id=request_id,
                error_code="QUERY_REQUEST_FAILED",
                retryable=False,
            ) from exc

        raw_path: Path | None = None
        try:
            data = response.json()
        except ValueError as exc:
            if raw_dir is not None:
                raw_path = persist_raw_text(
                    raw_dir,
                    f"{task_kind}_query",
                    getattr(response, "text", ""),
                    request_id=request_id,
                    extension="txt",
                )
            raise ProviderError(
                f"MiniMax {task_kind} query response is not JSON",
                provider=task_kind,
                status_code=int(getattr(response, "status_code", 0) or 0),
                request_id=request_id,
                raw_response_path=str(raw_path) if raw_path else None,
                error_code="INVALID_RESPONSE",
                retryable=False,
            ) from exc
        if raw_dir is not None:
            raw_path = persist_raw_json(
                raw_dir, f"{task_kind}_query", data, request_id=request_id
            )
        status_code = int(getattr(response, "status_code", 200) or 200)
        if status_code < 200 or status_code >= 300:
            raise ProviderError(
                f"MiniMax {task_kind} query HTTP {status_code}",
                provider=task_kind,
                status_code=status_code,
                request_id=request_id,
                raw_response_path=str(raw_path) if raw_path else None,
                payload=data,
                retryable=False,
            )
        if not isinstance(data, dict):
            raise ProviderError(
                f"MiniMax {task_kind} query response is not an object",
                provider=task_kind,
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

    def wait_task(
        self,
        task_id: str,
        *,
        task_kind: str = "minimax",
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
                f"MiniMax {task_kind} task wait timeout must be positive",
                provider=task_kind,
                error_code="TASK_TIMEOUT",
                retryable=False,
            )
        deadline = time.monotonic() + wait_seconds
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise PipelineCancelled(f"Cancellation requested while waiting for {task_kind}")
            if time.monotonic() >= deadline:
                raise ProviderError(
                    f"MiniMax {task_kind} task polling timed out",
                    provider=task_kind,
                    error_code="TASK_TIMEOUT",
                    retryable=False,
                )
            response = self.get_task(
                task_id,
                task_kind=task_kind,
                raw_dir=raw_dir,
            )
            status = task_status(response.data)
            if status == "succeeded":
                if task_kind == "minimax_context_ir" and not task_prompt(response.data):
                    raise ProviderError(
                        "MiniMax H3-Context-IR succeeded without task.content.prompt",
                        provider=task_kind,
                        request_id=response.request_id,
                        raw_response_path=(
                            str(response.raw_path) if response.raw_path else None
                        ),
                        error_code="PROMPT_MISSING",
                        retryable=False,
                    )
                if task_kind == "minimax_h3" and not task_video_url(response.data):
                    raise ProviderError(
                        "MiniMax H3 succeeded without task.content.url",
                        provider=task_kind,
                        request_id=response.request_id,
                        raw_response_path=(
                            str(response.raw_path) if response.raw_path else None
                        ),
                        error_code="VIDEO_URL_MISSING",
                        retryable=False,
                    )
                return response
            if status in {"failed", "cancelled", "expired"}:
                raise ProviderError(
                    f"MiniMax {task_kind} task ended with status {status}",
                    provider=task_kind,
                    error_code=status.upper(),
                    request_id=response.request_id,
                    raw_response_path=str(response.raw_path) if response.raw_path else None,
                    payload=response.data,
                    retryable=False,
                )
            if status not in {"queued", "running", "processing"}:
                raise ProviderError(
                    f"Unknown MiniMax {task_kind} task status: {status or '<empty>'}",
                    provider=task_kind,
                    error_code="UNKNOWN_STATUS",
                    request_id=response.request_id,
                    raw_response_path=str(response.raw_path) if response.raw_path else None,
                    payload=response.data,
                    retryable=False,
                )
            remaining = deadline - time.monotonic()
            if remaining > 0:
                self.sleeper(min(self.config.poll_interval, remaining))

    def check_access(self, *, raw_dir: Path | None = None) -> None:
        del raw_dir
        if not self.config.minimax_api_key:
            raise ProviderError(
                "MINIMAX_API_KEY is empty",
                provider="minimax",
                error_code="API_KEY_MISSING",
                retryable=False,
            )
        if self.config.minimax_model != FIXED_MINIMAX_H3_MODEL:
            raise ProviderError(
                f"MiniMax model must be {FIXED_MINIMAX_H3_MODEL}",
                provider="minimax",
                error_code="MODEL_NOT_SUPPORTED",
                retryable=False,
            )
