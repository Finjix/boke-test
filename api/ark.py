"""REST adapter for the configured Ark multimodal model."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from api.common import ApiResponse, response_request_id
from config import (
    AppConfig,
    FIXED_DOUBAO_MODEL,
    FIXED_SEEDREAM_MODEL,
    FIXED_SEEDREAM_SIZE,
)
from utils.artifacts import persist_raw_json, persist_raw_text
from utils.errors import ProviderError, ValidationError
from utils.ids import new_request_id
from utils.logger import JobLogger
from utils.retry import retry_call


def extract_text(response: ApiResponse | dict[str, Any]) -> str:
    data = response.data if isinstance(response, ApiResponse) else response
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValidationError("Ark response has no choices[0].message.content") from exc
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                pieces.append(item["text"])
        if pieces:
            return "".join(pieces)
    raise ValidationError("Ark message content is not text")


def extract_image_url(response: ApiResponse | dict[str, Any]) -> str:
    """Extract the first HTTPS image URL from a Seedream response."""

    data = response.data if isinstance(response, ApiResponse) else response
    items = data.get("data") if isinstance(data, dict) else None
    first = items[0] if isinstance(items, list) and items else None
    url = first.get("url") if isinstance(first, dict) else None
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValidationError("Seedream response has no HTTPS image URL")
    return url


@dataclass
class ArkClient:
    config: AppConfig
    logger: JobLogger | None = None
    session: requests.Session | None = None
    last_request_id: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.config.doubao_model != FIXED_DOUBAO_MODEL:
            raise ProviderError(
                f"Doubao model must be {FIXED_DOUBAO_MODEL}",
                provider="ark",
                error_code="MODEL_NOT_ALLOWED",
                retryable=False,
            )
        if self.session is None:
            self.session = requests.Session()

    @staticmethod
    def extract_text(response: ApiResponse | dict[str, Any]) -> str:
        return extract_text(response)

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        stage: str = "ark_chat",
        raw_dir: Path | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> ApiResponse:
        request_id = new_request_id()
        self.last_request_id = request_id
        payload = {
            "model": self.config.doubao_model,
            "messages": messages,
            "thinking": {"type": "disabled"},
            "stream": False,
        }
        if response_format is not None:
            payload["response_format"] = response_format

        def operation() -> ApiResponse:
            try:
                response = self.session.post(  # type: ignore[union-attr]
                    f"{self.config.ark_base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.config.ark_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self.config.http_timeout,
                )
            except requests.exceptions.RequestException as exc:
                raise ProviderError(
                    f"Ark request failed: {exc}",
                    provider="ark",
                    request_id=request_id,
                    retryable=True,
                ) from exc

            raw_path: Path | None = None
            try:
                data = response.json()
                if raw_dir is not None:
                    raw_path = persist_raw_json(
                        raw_dir,
                        stage,
                        data,
                        request_id=request_id,
                    )
                if not isinstance(data, dict):
                    raise ProviderError(
                        "Ark response JSON is not an object",
                        provider="ark",
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
                        stage,
                        body,
                        request_id=request_id,
                        extension="txt",
                    )
                raise ProviderError(
                    "Ark returned non-JSON response",
                    provider="ark",
                    status_code=getattr(response, "status_code", None),
                    request_id=request_id,
                    raw_response_path=str(raw_path) if raw_path else None,
                    retryable=getattr(response, "status_code", 0) >= 500,
                ) from exc

            status = int(getattr(response, "status_code", 200))
            if status < 200 or status >= 300:
                raise ProviderError(
                    f"Ark HTTP {status}",
                    provider="ark",
                    status_code=status,
                    error_code=str(data.get("error", {}).get("code", ""))
                    if isinstance(data.get("error"), dict)
                    else None,
                    request_id=request_id,
                    raw_response_path=str(raw_path) if raw_path else None,
                    payload=data,
                )
            result = ApiResponse(
                data=data,
                request_id=response_request_id(data, request_id),
                raw_path=raw_path,
            )
            self.last_request_id = result.request_id
            return result

        return retry_call(
            operation,
            attempts=self.config.max_retries,
            on_retry=(
                lambda attempt, delay, error: self.logger.warning(
                    "retrying Ark request",
                    attempt=attempt,
                    delay=delay,
                    error=str(error),
                )
                if self.logger
                else None
            ),
        )

    def generate_image(
        self,
        image_urls: list[str],
        prompt: str,
        *,
        stage: str = "seedream",
        raw_dir: Path | None = None,
        size: str = FIXED_SEEDREAM_SIZE,
        watermark: bool = False,
    ) -> ApiResponse:
        """Create one Seedream image-edit result.

        Image creation is deliberately not retried here: a transport failure
        cannot prove that the provider did not accept a paid generation.
        Callers persist the failed attempt and require an explicit retry.
        """

        if not prompt.strip():
            raise ProviderError(
                "Seedream prompt cannot be empty",
                provider="seedream",
                error_code="PROMPT_EMPTY",
                retryable=False,
            )
        values = [str(url).strip() for url in image_urls if str(url).strip()]
        if not values:
            raise ProviderError(
                "Seedream requires at least one source image",
                provider="seedream",
                error_code="IMAGE_MISSING",
                retryable=False,
            )
        if any(not url.startswith("https://") for url in values):
            raise ProviderError(
                "Seedream source images must use HTTPS URLs",
                provider="seedream",
                error_code="IMAGE_URL_INVALID",
                retryable=False,
            )
        if self.config.seedream_model != FIXED_SEEDREAM_MODEL:
            raise ProviderError(
                f"Seedream model must be {FIXED_SEEDREAM_MODEL}",
                provider="seedream",
                error_code="MODEL_NOT_ALLOWED",
                retryable=False,
            )
        if not self.config.ark_api_key:
            raise ProviderError(
                "ARK_API_KEY is empty",
                provider="seedream",
                error_code="API_KEY_MISSING",
                retryable=False,
            )
        request_id = new_request_id()
        self.last_request_id = request_id
        payload: dict[str, Any] = {
            "model": self.config.seedream_model,
            "prompt": prompt.strip(),
            "image": values[0] if len(values) == 1 else values,
            "size": size,
            "stream": False,
            "response_format": "url",
            "watermark": bool(watermark),
        }
        try:
            response = self.session.post(  # type: ignore[union-attr]
                f"{self.config.ark_base_url}/images/generations",
                headers={
                    "Authorization": f"Bearer {self.config.ark_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.config.http_timeout,
            )
        except requests.exceptions.RequestException as exc:
            raise ProviderError(
                f"Seedream request outcome is unknown: {exc}",
                provider="seedream",
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
                    stage,
                    getattr(response, "text", ""),
                    request_id=request_id,
                    extension="txt",
                )
            raise ProviderError(
                "Seedream response is not JSON",
                provider="seedream",
                status_code=int(getattr(response, "status_code", 0) or 0),
                request_id=request_id,
                raw_response_path=str(raw_path) if raw_path else None,
                error_code="INVALID_RESPONSE",
                retryable=False,
            ) from exc
        if raw_dir is not None:
            raw_path = persist_raw_json(raw_dir, stage, data, request_id=request_id)
        status_code = int(getattr(response, "status_code", 200) or 200)
        if status_code < 200 or status_code >= 300:
            error = data.get("error") if isinstance(data, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            raise ProviderError(
                str(message or f"Seedream HTTP {status_code}"),
                provider="seedream",
                status_code=status_code,
                request_id=request_id,
                raw_response_path=str(raw_path) if raw_path else None,
                error_code=str(error.get("code")) if isinstance(error, dict) and error.get("code") else None,
                payload=data,
            )
        if not isinstance(data, dict):
            raise ProviderError(
                "Seedream response is not an object",
                provider="seedream",
                request_id=request_id,
                raw_response_path=str(raw_path) if raw_path else None,
                error_code="INVALID_RESPONSE_OBJECT",
                retryable=False,
            )
        result = ApiResponse(
            data=data,
            request_id=response_request_id(data, request_id),
            raw_path=raw_path,
        )
        self.last_request_id = result.request_id
        return result
