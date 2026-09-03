"""Streaming downloads for provider-produced temporary media URLs."""

from __future__ import annotations

from pathlib import Path

import requests

from utils.errors import ProviderError


def download(
    url: str,
    output_path: Path,
    *,
    timeout: float = 180.0,
    session: requests.Session | None = None,
) -> Path:
    if not url.startswith(("http://", "https://")):
        raise ProviderError(
            "Download URL must use HTTP(S)",
            provider="downloader",
            error_code="INVALID_DOWNLOAD_URL",
            retryable=False,
        )
    client = session or requests.Session()

    def operation() -> Path:
        try:
            response = client.get(url, stream=True, timeout=timeout)
            response.raise_for_status()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with output_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)
        except requests.exceptions.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            raise ProviderError(
                f"Download failed: {exc}",
                provider="downloader",
                status_code=status,
                retryable=status is None or status == 429 or status >= 500,
            ) from exc
        return output_path

    return operation()
