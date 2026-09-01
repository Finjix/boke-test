"""Strict JSON parsing for model responses and CLI output."""

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


def parse_cli_json(stdout: str) -> Any:
    """Extract the business JSON while allowing CLI log lines around it."""

    text = stdout.strip()
    if not text:
        raise JsonContractError("MediaKit CLI returned no JSON")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    candidates: list[Any] = []
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        candidates.append(value)
    if not candidates:
        raise JsonContractError("MediaKit CLI stdout contains no JSON object")
    for value in reversed(candidates):
        if isinstance(value, dict) and any(key in value for key in ("task_id", "result", "id")):
            return value
    return candidates[-1]
