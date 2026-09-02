"""Seed Audio 1.0 full-scene localization adapter."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from api.common import ApiResponse, response_request_id
from config import AppConfig, FIXED_SEED_AUDIO_MODEL
from utils.artifacts import persist_raw_json, persist_raw_text
from utils.errors import ProviderError
from utils.ids import new_request_id
from utils.logger import JobLogger
from utils.retry import retry_call
from video_config import AUDIO_SAMPLE_RATE


def _https(value: str, label: str) -> str:
    if not value.startswith("https://"):
        raise ProviderError(
            f"{label} must be an HTTPS URL",
            provider="seed-audio",
            error_code="INVALID_REFERENCE_URL",
            retryable=False,
        )
    return value


@dataclass(frozen=True)
class GeneratedAudio:
    output_path: Path
    request_id: str
    raw: dict[str, Any]
    raw_path: Path | None = None


@dataclass
class SeedAudioClient:
    config: AppConfig
    logger: JobLogger | None = None
    session: requests.Session | None = None

    def __post_init__(self) -> None:
        if self.config.seed_audio_model != FIXED_SEED_AUDIO_MODEL:
            raise ProviderError(
                f"Seed-Audio model must be {FIXED_SEED_AUDIO_MODEL}",
                provider="seed-audio",
                error_code="MODEL_NOT_ALLOWED",
            )
        if self.session is None:
            self.session = requests.Session()

    def build_payload(self, prompt: str, reference_audio_url: str) -> dict[str, Any]:
        if not prompt.strip():
            raise ProviderError(
                "Seed Audio prompt cannot be empty",
                provider="seed-audio",
                error_code="PROMPT_EMPTY",
                retryable=False,
            )
        return {
            "model": FIXED_SEED_AUDIO_MODEL,
            "text_prompt": prompt,
            "references": [{"audio_url": _https(reference_audio_url, "reference audio")}],
            "audio_config": {
                "format": "wav",
                "sample_rate": AUDIO_SAMPLE_RATE,
                "speech_rate": 0,
                "loudness_rate": 0,
                "pitch_rate": 0,
            },
            "watermark": {},
        }

    def generate_localized_audio(
        self,
        prompt: str,
        reference_audio_url: str,
        output_path: Path,
        *,
        raw_dir: Path | None = None,
    ) -> GeneratedAudio:
        request_id = new_request_id()
        payload = self.build_payload(prompt, reference_audio_url)

        def operation() -> ApiResponse:
            try:
                response = self.session.post(  # type: ignore[union-attr]
                    self.config.seed_audio_endpoint,
                    headers={
                        "X-Api-Key": self.config.seed_audio_api_key,
                        "X-Api-Request-Id": request_id,
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self.config.http_timeout,
                )
            except requests.exceptions.RequestException as exc:
                raise ProviderError(
                    f"Seed-Audio request failed: {exc}",
                    provider="seed-audio",
                    request_id=request_id,
                    retryable=True,
                ) from exc

            raw_path: Path | None = None
            try:
                data = response.json()
                if raw_dir is not None:
                    raw_path = persist_raw_json(
                        raw_dir,
                        "seed_audio",
                        data,
                        request_id=request_id,
                    )
                if not isinstance(data, dict):
                    raise ProviderError(
                        "Seed-Audio response JSON is not an object",
                        provider="seed-audio",
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
                        "seed_audio",
                        body,
                        request_id=request_id,
                        extension="txt",
                    )
                raise ProviderError(
                    "Seed-Audio returned non-JSON response",
                    provider="seed-audio",
                    status_code=getattr(response, "status_code", None),
                    request_id=request_id,
                    raw_response_path=str(raw_path) if raw_path else None,
                ) from exc

            status = int(getattr(response, "status_code", 200))
            if status < 200 or status >= 300:
                raise ProviderError(
                    f"Seed-Audio HTTP {status}",
                    provider="seed-audio",
                    status_code=status,
                    request_id=request_id,
                    raw_response_path=str(raw_path) if raw_path else None,
                    payload=data,
                )
            code = data.get("code")
            if code not in (None, 0, "0", "OK", "ok"):
                raise ProviderError(
                    f"Seed-Audio returned error code {code}",
                    provider="seed-audio",
                    error_code=str(code),
                    request_id=request_id,
                    raw_response_path=str(raw_path) if raw_path else None,
                    payload=data,
                    retryable=False,
                )
            return ApiResponse(
                data=data,
                request_id=response_request_id(data, request_id),
                raw_path=raw_path,
            )

        response = retry_call(
            operation,
            attempts=self.config.max_retries,
            on_retry=(
                lambda attempt, delay, error: self.logger.warning(
                    "retrying Seed-Audio request",
                    attempt=attempt,
                    delay=delay,
                    error=str(error),
                )
                if self.logger
                else None
            ),
        )
        data = response.data
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ProviderError(
                f"Seed-Audio output directory could not be created: {exc}",
                provider="seed-audio",
                request_id=response.request_id,
                raw_response_path=str(response.raw_path) if response.raw_path else None,
                error_code="OUTPUT_DIRECTORY_FAILED",
                retryable=False,
            ) from exc
        if data.get("audio"):
            try:
                output_path.write_bytes(base64.b64decode(data["audio"], validate=True))
            except (binascii.Error, ValueError, TypeError) as exc:
                raise ProviderError(
                    "Seed-Audio returned invalid Base64 audio",
                    provider="seed-audio",
                    request_id=response.request_id,
                    raw_response_path=str(response.raw_path) if response.raw_path else None,
                    error_code="INVALID_AUDIO_BASE64",
                    retryable=False,
                ) from exc
            except OSError as exc:
                raise ProviderError(
                    f"Seed-Audio output could not be written: {exc}",
                    provider="seed-audio",
                    request_id=response.request_id,
                    raw_response_path=str(response.raw_path) if response.raw_path else None,
                    error_code="OUTPUT_WRITE_FAILED",
                    retryable=False,
                ) from exc
        elif data.get("url"):
            url = str(data["url"])
            if not url.startswith(("http://", "https://")):
                raise ProviderError(
                    "Seed-Audio returned an invalid audio URL",
                    provider="seed-audio",
                    request_id=response.request_id,
                    error_code="INVALID_AUDIO_URL",
                    retryable=False,
                )
            def download_audio() -> bytes:
                try:
                    download_response = self.session.get(  # type: ignore[union-attr]
                        url,
                        timeout=self.config.http_timeout,
                    )
                    download_response.raise_for_status()
                except requests.exceptions.RequestException as exc:
                    status = getattr(
                        getattr(exc, "response", None),
                        "status_code",
                        None,
                    )
                    raise ProviderError(
                        f"Seed-Audio audio download failed: {exc}",
                        provider="seed-audio",
                        status_code=status,
                        request_id=response.request_id,
                        raw_response_path=(
                            str(response.raw_path) if response.raw_path else None
                        ),
                        retryable=status is None or status == 429 or status >= 500,
                    ) from exc
                return bytes(download_response.content)

            audio_bytes = retry_call(
                download_audio,
                attempts=self.config.max_retries,
                on_retry=(
                    lambda attempt, delay, error: self.logger.warning(
                        "retrying Seed-Audio audio download",
                        attempt=attempt,
                        delay=delay,
                        error=str(error),
                    )
                    if self.logger
                    else None
                ),
            )
            try:
                output_path.write_bytes(audio_bytes)
            except OSError as exc:
                raise ProviderError(
                    f"Seed-Audio output could not be written: {exc}",
                    provider="seed-audio",
                    request_id=response.request_id,
                    raw_response_path=str(response.raw_path) if response.raw_path else None,
                    error_code="OUTPUT_WRITE_FAILED",
                    retryable=False,
                ) from exc
        else:
            raise ProviderError(
                "Seed-Audio response has neither audio nor url",
                provider="seed-audio",
                request_id=response.request_id,
                raw_response_path=str(response.raw_path) if response.raw_path else None,
                error_code="NO_AUDIO_RESULT",
                payload=data,
                retryable=False,
            )
        return GeneratedAudio(
            output_path=output_path,
            request_id=response.request_id,
            raw=data,
            raw_path=response.raw_path,
        )
