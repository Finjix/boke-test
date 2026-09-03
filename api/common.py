"""Shared response type for HTTP provider calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ApiResponse:
    data: dict[str, Any]
    request_id: str = ""


def response_request_id(data: dict[str, Any]) -> str:
    value = data.get("request_id") or data.get("requestId")
    return str(value) if value else ""
