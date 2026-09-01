"""REST adapter for the configured Ark Chat model."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from api.common import ApiResponse, response_request_id
from config import AppConfig
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


@dataclass
class ArkClient:
    config: AppConfig
    logger: JobLogger | None = None
    session: requests.Session | None = None
    last_request_id: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
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
    ) -> ApiResponse:
        request_id = new_request_id()
        self.last_request_id = request_id
        payload = {
            "model": self.config.doubao_model,
            "messages": messages,
            "thinking": {"type": "disabled"},
            "stream": False,
        }

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

    def check_access(self, raw_dir: Path | None = None) -> ApiResponse:
        return self.chat(
            [
                {"role": "system", "content": "只输出 JSON。"},
                {"role": "user", "content": "返回 {\"ok\":true}。"},
            ],
            stage="preflight_ark_chat",
            raw_dir=raw_dir,
        )
