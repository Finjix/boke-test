"""Stable identifiers for jobs and provider requests."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4


def new_request_id() -> str:
    return str(uuid4())


def new_job_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"job_{timestamp}_{uuid4().hex[:8]}"
