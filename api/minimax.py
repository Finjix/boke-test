"""MiniMax H3-Context-IR and H3 video API adapter."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests

from api.common import ApiResponse, response_request_id
from config import (
    FIXED_MINIMAX_H3_MODEL,
    MINIMAX_GENERATION_MAX_DURATION_SECONDS,
    MINIMAX_GENERATION_MIN_DURATION_SECONDS,
    MINIMAX_H3_RESOLUTIONS,
    AppConfig,
)
from core.h3_prompt import ensure_payload_size
from utils.errors import PipelineCancelled, ProviderError, ValidationError


@dataclass(frozen=True)
class MiniMaxTask:
    task_id: str
    request_id: str = ""


def _task_payload(data: Any) -> dict[str, Any]:
    if isinstance(data, dict) and isinstance(data.get("task"), dict):
        return data["task"]
    return data if isinstance(data, dict) else {}


def task_status(data: Any) -> str:
    return str(_task_payload(data).get("status") or "").strip().lower()


def task_video_url(data: Any) -> str | None:
    content = _task_payload(data).get("content")
    if isinstance(content, dict):
        value = content.get("url")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def task_prompt(data: Any) -> str | None:
    content = _task_payload(data).get("content")
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
        session: requests.Session | None = None,
        sleeper: Any = time.sleep,
    ) -> None:
        self.config = config
        self.sleeper = sleeper
        self.session = session or requests.Session()
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

    def _validate_common(self, content: list[dict[str, object]], duration: int) -> None:
        if self.config.minimax_model != FIXED_MINIMAX_H3_MODEL:
            raise ProviderError(
                f"模型必须为 {FIXED_MINIMAX_H3_MODEL}",
                provider="minimax",
                error_code="MODEL_NOT_SUPPORTED",
            )
        if not self.config.minimax_api_key.strip():
            raise ProviderError(
                "MiniMax API Key 为空",
                provider="minimax",
                error_code="API_KEY_MISSING",
            )
        if not isinstance(content, list) or not content:
            raise ProviderError(
                "MiniMax content 为空",
                provider="minimax",
                error_code="CONTENT_EMPTY",
            )
        if not any(
            isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
            and item["text"].strip()
            for item in content
        ):
            raise ProviderError(
                "MiniMax content 缺少文本提示词",
                provider="minimax",
                error_code="PROMPT_EMPTY",
            )
        if (
            not isinstance(duration, int)
            or isinstance(duration, bool)
            or not MINIMAX_GENERATION_MIN_DURATION_SECONDS
            <= duration
            <= MINIMAX_GENERATION_MAX_DURATION_SECONDS
        ):
            raise ProviderError(
                "MiniMax 生成时长必须为 4–15 秒整数",
                provider="minimax",
                error_code="INVALID_DURATION",
            )

    def _create_task(
        self,
        *,
        endpoint: str,
        payload: dict[str, object],
        task_kind: str,
    ) -> MiniMaxTask:
        ensure_payload_size(payload)
        try:
            response = self.session.post(
                f"{self.base_url}{endpoint}",
                headers=self._headers(),
                json=payload,
                timeout=self.config.http_timeout,
            )
        except requests.exceptions.RequestException as exc:
            raise ProviderError(
                f"{task_kind} 请求失败",
                provider=task_kind,
                error_code="CREATE_REQUEST_FAILED",
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(
                f"{task_kind} 返回内容无效",
                provider=task_kind,
                status_code=int(getattr(response, "status_code", 0) or 0),
                error_code="INVALID_RESPONSE",
            ) from exc
        status_code = int(getattr(response, "status_code", 200) or 200)
        request_id = response_request_id(data) if isinstance(data, dict) else ""
        if status_code < 200 or status_code >= 300:
            detail = ""
            if isinstance(data, dict):
                error = data.get("error")
                if isinstance(error, dict):
                    detail = str(error.get("message") or "")
            suffix = f": {detail}" if detail else ""
            raise ProviderError(
                f"{task_kind} 请求被拒绝{suffix}",
                provider=task_kind,
                status_code=status_code,
                request_id=request_id,
                error_code="CREATE_FAILED",
            )
        if not isinstance(data, dict):
            raise ProviderError(
                f"{task_kind} 返回格式无效",
                provider=task_kind,
                error_code="INVALID_RESPONSE_OBJECT",
            )
        task_id = data.get("task_id") or data.get("id")
        if not task_id and isinstance(data.get("task"), dict):
            task_id = data["task"].get("task_id") or data["task"].get("id")
        if not task_id:
            raise ProviderError(
                f"{task_kind} 未返回任务 ID",
                provider=task_kind,
                request_id=request_id,
                error_code="TASK_ID_MISSING",
            )
        return MiniMaxTask(str(task_id), request_id)

    def create_context_ir_task(
        self,
        content: list[dict[str, object]],
        *,
        duration: int,
        ratio: str = "adaptive",
    ) -> MiniMaxTask:
        self._validate_common(content, duration)
        if not ratio:
            raise ValidationError("Context-IR 比例不能为空")
        return self._create_task(
            endpoint="/v2/h3_context_ir",
            payload={
                "model": FIXED_MINIMAX_H3_MODEL,
                "content": content,
                "duration": duration,
                "ratio": ratio,
            },
            task_kind="H3-Context-IR",
        )

    def create_video_task(
        self,
        content: list[dict[str, object]],
        *,
        duration: int,
        resolution: str | None = None,
        ratio: str = "adaptive",
    ) -> MiniMaxTask:
        self._validate_common(content, duration)
        chosen_resolution = (resolution or self.config.minimax_resolution).upper()
        if chosen_resolution not in MINIMAX_H3_RESOLUTIONS:
            raise ProviderError(
                "H3 分辨率必须为 768P 或 2K",
                provider="minimax_h3",
                error_code="INVALID_RESOLUTION",
            )
        if not ratio:
            raise ValidationError("H3 比例不能为空")
        return self._create_task(
            endpoint="/v2/video_generation",
            payload={
                "model": FIXED_MINIMAX_H3_MODEL,
                "content": content,
                "duration": duration,
                "resolution": chosen_resolution,
                "ratio": ratio,
            },
            task_kind="MiniMax-H3",
        )

    def get_task(self, task_id: str, *, task_kind: str) -> ApiResponse:
        try:
            response = self.session.get(
                f"{self.base_url}/v2/query/video_generation/{task_id}",
                headers=self._headers(),
                timeout=self.config.http_timeout,
            )
        except requests.exceptions.RequestException as exc:
            raise ProviderError(
                f"{task_kind} 查询失败",
                provider=task_kind,
                error_code="QUERY_REQUEST_FAILED",
            ) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(
                f"{task_kind} 查询返回内容无效",
                provider=task_kind,
                status_code=int(getattr(response, "status_code", 0) or 0),
                error_code="INVALID_RESPONSE",
            ) from exc
        status_code = int(getattr(response, "status_code", 200) or 200)
        request_id = response_request_id(data) if isinstance(data, dict) else ""
        if status_code < 200 or status_code >= 300 or not isinstance(data, dict):
            raise ProviderError(
                f"{task_kind} 查询失败",
                provider=task_kind,
                status_code=status_code,
                request_id=request_id,
                error_code="QUERY_FAILED",
            )
        return ApiResponse(data=data, request_id=request_id)

    def wait_task(
        self,
        task_id: str,
        *,
        task_kind: str,
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
                f"{task_kind} 等待超时配置无效",
                provider=task_kind,
                error_code="TASK_TIMEOUT",
            )
        deadline = time.monotonic() + wait_seconds
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise PipelineCancelled(f"正在等待 {task_kind} 时已取消")
            if time.monotonic() >= deadline:
                raise ProviderError(
                    f"{task_kind} 等待超时",
                    provider=task_kind,
                    error_code="TASK_TIMEOUT",
                )
            response = self.get_task(task_id, task_kind=task_kind)
            status = task_status(response.data)
            if status == "succeeded":
                if task_kind == "H3-Context-IR" and not task_prompt(response.data):
                    raise ProviderError(
                        "H3-Context-IR 未返回结构提示词",
                        provider=task_kind,
                        request_id=response.request_id,
                        error_code="PROMPT_MISSING",
                    )
                if task_kind == "MiniMax-H3" and not task_video_url(response.data):
                    raise ProviderError(
                        "MiniMax-H3 未返回视频地址",
                        provider=task_kind,
                        request_id=response.request_id,
                        error_code="VIDEO_URL_MISSING",
                    )
                return response
            if status in {"failed", "cancelled", "expired"}:
                raise ProviderError(
                    f"{task_kind} 处理失败",
                    provider=task_kind,
                    request_id=response.request_id,
                    error_code=status.upper(),
                )
            if status not in {"queued", "running", "processing"}:
                raise ProviderError(
                    f"{task_kind} 返回未知状态",
                    provider=task_kind,
                    request_id=response.request_id,
                    error_code="UNKNOWN_STATUS",
                )
            remaining = deadline - time.monotonic()
            if remaining > 0:
                self.sleeper(min(self.config.poll_interval, remaining))
