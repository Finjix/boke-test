"""Strict JSON parsing for model responses."""

from __future__ import annotations

import json
from typing import Any

from utils.errors import JsonContractError


def parse_strict_json(content: str, *, description: str = "model response") -> Any:
    text = content.strip()
    if not text:
        raise JsonContractError(f"{description} is empty")
    if text.startswith("```") or not (text.startswith("{") or text.startswith("[")):
        raise JsonContractError(f"{description} contains non-JSON text")
    try:
        value, end = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError as exc:
        raise JsonContractError(f"Invalid JSON in {description}: {exc.msg}") from exc
    if text[end:].strip():
        raise JsonContractError(f"{description} contains trailing explanation text")
    return value
