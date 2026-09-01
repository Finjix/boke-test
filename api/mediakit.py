"""Official MediaKit CLI adapter.

No MediaKit REST endpoint is embedded here.  The CLI remains the sole cloud
entry point, and its current schema can be inspected without submitting a
business task.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import AppConfig
from utils.artifacts import persist_raw_json, persist_raw_text, write_json
from utils.errors import JsonContractError, ProviderError
from utils.ids import new_request_id
from utils.json_parser import parse_cli_json
from utils.logger import JobLogger
from utils.retry import retry_call


@dataclass(frozen=True)
class MediaKitTask:
    task_id: str
    request_id: str | None
    raw: dict[str, Any]

    @property
    def result(self) -> dict[str, Any]:
        result = self.raw.get("result")
        return result if isinstance(result, dict) else {}


def _task_id(value: dict[str, Any]) -> str:
    task_id = value.get("task_id") or value.get("id")
    if not task_id and isinstance(value.get("data"), dict):
        task_id = value["data"].get("task_id") or value["data"].get("id")
    if not task_id:
        raise JsonContractError("MediaKit response has no task_id")
    return str(task_id)


@dataclass
class MediaKitClient:
    config: AppConfig
    logger: JobLogger | None = None

    def _run_once(
        self,
        args: list[str],
        *,
        stage: str,
        raw_dir: Path | None,
    ) -> dict[str, Any]:
        request_id = new_request_id()
        env = os.environ.copy()
        if self.config.mediakit_api_key:
            env["MEDIAKIT_API_KEY"] = self.config.mediakit_api_key
        try:
            completed = subprocess.run(
                [self.config.mediakit_cli_bin, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.config.http_timeout,
                check=False,
                env=env,
            )
        except FileNotFoundError as exc:
            raise ProviderError(
                f"MediaKit CLI not found: {self.config.mediakit_cli_bin}",
                provider="mediakit",
                error_code="CLI_NOT_FOUND",
                request_id=request_id,
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(
                "MediaKit CLI command timed out",
                provider="mediakit",
                error_code="CLI_TIMEOUT",
                request_id=request_id,
                retryable=True,
            ) from exc
        except OSError as exc:
            raise ProviderError(
                f"MediaKit CLI could not start: {exc}",
                provider="mediakit",
                error_code="CLI_START_FAILED",
                request_id=request_id,
            ) from exc

        stdout_path: Path | None = None
        stderr_path: Path | None = None
        meta_path: Path | None = None
        if raw_dir is not None:
            stdout_path = persist_raw_text(
                raw_dir,
                f"{stage}_stdout",
                completed.stdout,
                request_id=request_id,
            )
            stderr_path = persist_raw_text(
                raw_dir,
                f"{stage}_stderr",
                completed.stderr,
                request_id=request_id,
            )
            meta_path = write_json(
                raw_dir / f"{stage}_{request_id}_meta.json",
                {
                    "command": [self.config.mediakit_cli_bin, *args],
                    "returncode": completed.returncode,
                },
            )

        if completed.returncode != 0:
            raise ProviderError(
                f"MediaKit CLI exited with code {completed.returncode}",
                provider="mediakit",
                error_code="CLI_EXIT",
                retryable=False,
                request_id=request_id,
                raw_response_path=(str(stderr_path) if stderr_path else str(meta_path) if meta_path else None),
                payload={"stderr": completed.stderr},
            )
        try:
            data = parse_cli_json(completed.stdout)
        except JsonContractError as exc:
            raise ProviderError(
                str(exc),
                provider="mediakit",
                error_code="CLI_INVALID_JSON",
                request_id=request_id,
                raw_response_path=str(stdout_path) if stdout_path else None,
                payload={"stdout": completed.stdout},
            ) from exc
        if not isinstance(data, dict):
            raise ProviderError(
                "MediaKit CLI business output is not an object",
                provider="mediakit",
                error_code="CLI_INVALID_CONTRACT",
                request_id=request_id,
                raw_response_path=str(stdout_path) if stdout_path else None,
            )
        if raw_dir is not None:
            persist_raw_json(raw_dir, stage, data, request_id=request_id)
        return data

    def _run(
        self,
        args: list[str],
        *,
        stage: str,
        raw_dir: Path | None,
    ) -> dict[str, Any]:
        return retry_call(
            lambda: self._run_once(args, stage=stage, raw_dir=raw_dir),
            attempts=self.config.max_retries,
            on_retry=(
                lambda attempt, delay, error: self.logger.warning(
                    "retrying MediaKit CLI command",
                    attempt=attempt,
                    delay=delay,
                    error=str(error),
                )
                if self.logger
                else None
            ),
        )

    def schema(self, domain: str, tool: str, *, raw_dir: Path | None = None) -> dict[str, Any]:
        return self._run([domain, tool, "--schema"], stage=f"schema_{domain}_{tool}", raw_dir=raw_dir)

    def doctor(self, *, raw_dir: Path | None = None) -> str:
        env = os.environ.copy()
        if self.config.mediakit_api_key:
            env["MEDIAKIT_API_KEY"] = self.config.mediakit_api_key
        request_id = new_request_id()
        try:
            completed = subprocess.run(
                [self.config.mediakit_cli_bin, "doctor"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.config.http_timeout,
                check=False,
                env=env,
            )
        except FileNotFoundError as exc:
            raise ProviderError(
                "MediaKit CLI is not installed",
                provider="mediakit",
                error_code="CLI_NOT_FOUND",
                request_id=request_id,
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(
                "MediaKit doctor timed out",
                provider="mediakit",
                error_code="CLI_TIMEOUT",
                request_id=request_id,
                retryable=True,
            ) from exc
        except OSError as exc:
            raise ProviderError(
                f"MediaKit doctor could not start: {exc}",
                provider="mediakit",
                error_code="CLI_START_FAILED",
                request_id=request_id,
            ) from exc
        stdout_path: Path | None = None
        stderr_path: Path | None = None
        if raw_dir is not None:
            stdout_path = persist_raw_text(
                raw_dir,
                "mediakit_doctor_stdout",
                completed.stdout,
                request_id=request_id,
            )
            stderr_path = persist_raw_text(
                raw_dir,
                "mediakit_doctor_stderr",
                completed.stderr,
                request_id=request_id,
            )
        if completed.returncode != 0:
            raise ProviderError(
                "MediaKit doctor failed",
                provider="mediakit",
                error_code="DOCTOR_FAILED",
                request_id=request_id,
                raw_response_path=(
                    str(stderr_path) if stderr_path else str(stdout_path) if stdout_path else None
                ),
                payload={"stderr": completed.stderr},
            )
        return completed.stdout

    def separate_voice(self, video_path: Path, *, raw_dir: Path | None = None) -> MediaKitTask:
        client_token = new_request_id()
        submit = self._run(
            [
                "--cloud",
                "audio",
                "separate-voice",
                "--video-url",
                str(video_path),
                "--output-format",
                "wav",
                "--client-token",
                client_token,
            ],
            stage="mediakit_separate_submit",
            raw_dir=raw_dir,
        )
        task_id = _task_id(submit)
        result = self._run(
            [
                "shared",
                "query-task",
                "--task-id",
                task_id,
                "--poll-complete",
            ],
            stage="mediakit_separate_query",
            raw_dir=raw_dir,
        )
        return MediaKitTask(
            task_id=task_id,
            request_id=str(submit.get("request_id")) if submit.get("request_id") else None,
            raw=result,
        )

    def asr(
        self,
        audio_path: Path,
        *,
        language: str = "eng-US",
        raw_dir: Path | None = None,
    ) -> MediaKitTask:
        client_token = new_request_id()
        submit = self._run(
            [
                "--cloud",
                "video",
                "asr-subtitles",
                "--audio-url",
                str(audio_path),
                "--content-type",
                "speech",
                "--language",
                language,
                "--enable-speaker-info",
                "--enable-confidence",
                "--client-token",
                client_token,
            ],
            stage="mediakit_asr_submit",
            raw_dir=raw_dir,
        )
        task_id = _task_id(submit)
        result = self._run(
            [
                "shared",
                "query-task",
                "--task-id",
                task_id,
                "--poll-complete",
            ],
            stage="mediakit_asr_query",
            raw_dir=raw_dir,
        )
        return MediaKitTask(
            task_id=task_id,
            request_id=str(submit.get("request_id")) if submit.get("request_id") else None,
            raw=result,
        )
