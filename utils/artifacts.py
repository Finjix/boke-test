"""Job artifact and raw-provider-response persistence."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:100] or "response"


def write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return path


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_text(path: Path, value: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


def persist_raw_json(
    raw_dir: Path,
    stage: str,
    payload: Any,
    *,
    request_id: str | None = None,
    attempt: int | None = None,
) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    suffix = "_".join(
        part
        for part in (str(attempt) if attempt is not None else "", request_id or "")
        if part
    )
    name = "_".join(part for part in (_safe_name(stage), stamp, suffix) if part)
    return write_json(raw_dir / f"{name}.json", payload)


def persist_raw_text(
    raw_dir: Path,
    stage: str,
    content: str,
    *,
    request_id: str | None = None,
    extension: str = "log",
) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    name = "_".join(part for part in (_safe_name(stage), stamp, request_id or "") if part)
    return write_text(raw_dir / f"{name}.{extension.lstrip('.')}", content)
