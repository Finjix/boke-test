"""Uguu temporary HTTPS asset upload adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

from config import AppConfig
from core.models import UploadedAsset
from utils.artifacts import persist_raw_json, persist_raw_text
from utils.errors import ProviderError
from utils.logger import JobLogger
from utils.retry import retry_call


@dataclass
class UguuClient:
    config: AppConfig
    logger: JobLogger | None = None
    session: requests.Session | None = None

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = requests.Session()

    def _validate_size(self, path: Path) -> None:
        if not path.is_file():
            raise ProviderError(
                f"Uguu upload source does not exist: {path}",
                provider="uguu",
                error_code="LOCAL_FILE_NOT_FOUND",
                retryable=False,
            )
        max_bytes = self.config.uguu_max_file_mib * 1024 * 1024
        if path.stat().st_size > max_bytes:
            raise ProviderError(
                f"File exceeds configured Uguu limit: {path.name}",
                provider="uguu",
                status_code=413,
                error_code="FILE_TOO_LARGE",
                retryable=False,
            )

    def upload(
        self,
        path: Path,
        *,
        kind: str = "reference",
        raw_dir: Path | None = None,
    ) -> UploadedAsset:
        path = Path(path)
        self._validate_size(path)

        def operation() -> UploadedAsset:
            raw_path = None
            try:
                with path.open("rb") as handle:
                    response = self.session.post(  # type: ignore[union-attr]
                        self.config.uguu_upload_url,
                        files={"files[]": (path.name, handle)},
                        timeout=self.config.http_timeout,
                    )
                status = int(getattr(response, "status_code", 200))
                try:
                    data = response.json()
                except ValueError as exc:
                    if raw_dir is not None:
                        raw_path = persist_raw_text(
                            raw_dir,
                            "uguu_upload",
                            getattr(response, "text", ""),
                            extension="txt",
                        )
                    raise ProviderError(
                        "Uguu returned non-JSON response",
                        provider="uguu",
                        status_code=status,
                        raw_response_path=str(raw_path) if raw_path else None,
                        retryable=status >= 500,
                    ) from exc
            except requests.exceptions.RequestException as exc:
                raise ProviderError(
                    f"Uguu upload failed: {exc}",
                    provider="uguu",
                    retryable=True,
                ) from exc
            except OSError as exc:
                raise ProviderError(
                    f"Uguu upload source could not be read: {exc}",
                    provider="uguu",
                    error_code="LOCAL_FILE_READ_FAILED",
                    retryable=False,
                ) from exc

            if raw_dir is not None:
                raw_path = persist_raw_json(raw_dir, "uguu_upload", data)
            if status < 200 or status >= 300:
                raise ProviderError(
                    f"Uguu HTTP {status}",
                    provider="uguu",
                    status_code=status,
                    raw_response_path=str(raw_path) if raw_path else None,
                    retryable=status == 429 or status >= 500,
                    payload=data,
                )
            files = data.get("files") if isinstance(data, dict) else None
            first_file = files[0] if isinstance(files, list) and files else None
            url = first_file.get("url") if isinstance(first_file, dict) else None
            if (
                not isinstance(data, dict)
                or data.get("success") is not True
                or not isinstance(url, str)
            ):
                raise ProviderError(
                    "Uguu response has no valid file URL",
                    provider="uguu",
                    error_code="INVALID_UPLOAD_RESPONSE",
                    raw_response_path=str(raw_path) if raw_path else None,
                    payload=data,
                    retryable=False,
                )
            if not url.startswith("https://"):
                raise ProviderError(
                    "Uguu returned a non-HTTPS URL",
                    provider="uguu",
                    error_code="NON_HTTPS_UPLOAD_URL",
                    raw_response_path=str(raw_path) if raw_path else None,
                    payload=data,
                    retryable=False,
                )
            return UploadedAsset(
                local_path=path,
                remote_url=url,
                uploaded_at=datetime.now(timezone.utc).isoformat(),
                kind=kind,
            )

        return retry_call(
            operation,
            attempts=self.config.max_retries,
            on_retry=(
                lambda attempt, delay, error: self.logger.warning(
                    "retrying Uguu upload",
                    attempt=attempt,
                    delay=delay,
                    error=str(error),
                )
                if self.logger
                else None
            ),
        )

    def upload_many(
        self,
        paths: list[tuple[Path, str]],
        *,
        raw_dir: Path | None = None,
    ) -> list[UploadedAsset]:
        return [
            self.upload(path, kind=kind, raw_dir=raw_dir)
            for path, kind in paths
        ]

    def check_access(self, *, raw_dir: Path | None = None) -> None:
        base = self.config.uguu_upload_url.rsplit("/", 1)[0]

        def operation() -> None:
            raw_path = None
            try:
                response = self.session.get(  # type: ignore[union-attr]
                    base,
                    timeout=self.config.http_timeout,
                )
                status = int(getattr(response, "status_code", 200))
                try:
                    data = response.json()
                    if raw_dir is not None:
                        raw_path = persist_raw_json(raw_dir, "preflight_uguu", data)
                except ValueError as exc:
                    if raw_dir is not None:
                        raw_path = persist_raw_text(
                            raw_dir,
                            "preflight_uguu",
                            getattr(response, "text", ""),
                            extension="txt",
                        )
                    raise ProviderError(
                        "Uguu endpoint returned non-JSON response",
                        provider="uguu",
                        status_code=status,
                        raw_response_path=str(raw_path) if raw_path else None,
                        retryable=status == 429 or status >= 500,
                    ) from exc
            except requests.exceptions.RequestException as exc:
                raise ProviderError(
                    f"Uguu endpoint is not reachable: {exc}",
                    provider="uguu",
                    retryable=True,
                ) from exc
            if status < 200 or status >= 400:
                raise ProviderError(
                    f"Uguu endpoint check returned HTTP {status}",
                    provider="uguu",
                    status_code=status,
                    raw_response_path=str(raw_path) if raw_path else None,
                    retryable=status == 429 or status >= 500,
                )

        retry_call(
            operation,
            attempts=self.config.max_retries,
            on_retry=(
                lambda attempt, delay, error: self.logger.warning(
                    "retrying Uguu endpoint check",
                    attempt=attempt,
                    delay=delay,
                    error=str(error),
                )
                if self.logger
                else None
            ),
        )
