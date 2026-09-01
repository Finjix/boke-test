"""Small shared pieces for HTTP provider adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ApiResponse:
    data: dict[str, Any]
    request_id: str
    raw_path: Path | None = None


def response_request_id(data: dict[str, Any], generated: str) -> str:
    value = data.get("request_id") or data.get("requestId")
    return str(value) if value else generated
