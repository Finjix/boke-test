"""Streaming download helper for the provider-produced video."""

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
            "生成结果地址无效",
            provider="downloader",
            error_code="INVALID_DOWNLOAD_URL",
        )
    client = session or requests.Session()
    output_path = Path(output_path)
    temporary = output_path.with_name(f".{output_path.name}.download")
    try:
        response = client.get(url, stream=True, timeout=timeout)
        response.raise_for_status()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
        if not temporary.is_file() or temporary.stat().st_size <= 0:
            raise ProviderError(
                "生成结果为空",
                provider="downloader",
                error_code="EMPTY_DOWNLOAD",
            )
        temporary.replace(output_path)
    except requests.exceptions.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        raise ProviderError(
            "生成结果下载失败",
            provider="downloader",
            status_code=status,
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)
    return output_path
