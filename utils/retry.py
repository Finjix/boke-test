"""Bounded retry policy shared by network and cloud task adapters."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

from utils.errors import ProviderError


T = TypeVar("T")


def _is_transient_exception(exc: Exception) -> bool:
    if isinstance(exc, ProviderError):
        return exc.retryable
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    module = exc.__class__.__module__
    name = exc.__class__.__name__.lower()
    return module.startswith("requests.") and any(
        marker in name for marker in ("timeout", "connection", "chunked", "proxy")
    )


def retry_call(
    operation: Callable[[], T],
    *,
    attempts: int = 3,
    on_retry: Callable[[int, float, Exception], None] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> T:
    if attempts <= 0:
        raise ValueError("attempts must be positive")
    last_error: Exception | None = None
    delays = (1.0, 2.0, 4.0)
    for index in range(attempts):
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 - classification is intentional
            last_error = exc
            if index >= attempts - 1 or not _is_transient_exception(exc):
                raise
            delay = delays[min(index, len(delays) - 1)]
            if on_retry is not None:
                on_retry(index + 1, delay, exc)
            sleeper(delay)
    assert last_error is not None
    raise last_error
