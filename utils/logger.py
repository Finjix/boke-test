"""File and callback logger with conservative secret redaction."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable


_SECRET_KEY = re.compile(r"(api.?key|authorization|token|secret|password)", re.I)


def _safe_value(key: str, value: Any) -> Any:
    if _SECRET_KEY.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _safe_value(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_value(key, item) for item in value]
    return value


class JobLogger:
    def __init__(
        self,
        log_path: Path | None = None,
        *,
        callback: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.log_path = log_path
        self.callback = callback
        self._lock = Lock()

    def emit(self, level: str, message: str, **fields: Any) -> None:
        safe_fields = {key: _safe_value(key, value) for key, value in fields.items()}
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level.upper(),
            "message": message,
            **safe_fields,
        }
        line = json.dumps(event, ensure_ascii=False, default=str)
        with self._lock:
            if self.log_path is not None:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        if self.callback is not None:
            self.callback(event)

    def info(self, message: str, **fields: Any) -> None:
        self.emit("INFO", message, **fields)

    def warning(self, message: str, **fields: Any) -> None:
        self.emit("WARNING", message, **fields)

    def error(self, message: str, **fields: Any) -> None:
        self.emit("ERROR", message, **fields)
