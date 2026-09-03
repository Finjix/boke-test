"""Strict JSON parsing for model responses."""

from __future__ import annotations

import json
from typing import Any

from utils.errors import JsonContractError


def parse_strict_json(
    content: str,
    *,
    description: str = "model response",
    allow_trailing_explanation: bool = False,
) -> Any:
    """Parse one JSON value, optionally allowing plain text after it.

    The default remains strict: model responses must contain JSON only.  The
    narrow opt-in is for providers that return one complete JSON object and
    then append a human-readable explanation.  A second JSON value, a code
    fence, or JSON punctuation that indicates a malformed continuation is
    still rejected.
    """

    text = content.strip()
    if not text:
        raise JsonContractError(f"{description} is empty")
    if text.startswith("```") or not (text.startswith("{") or text.startswith("[")):
        raise JsonContractError(f"{description} contains non-JSON text")
    try:
        value, end = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError as exc:
        raise JsonContractError(f"Invalid JSON in {description}: {exc.msg}") from exc
    suffix = text[end:].strip()
    if suffix:
        if not allow_trailing_explanation:
            raise JsonContractError(f"{description} contains trailing explanation text")
        if suffix.startswith("```") or suffix[0] in "{[\"0123456789-,:]}":
            raise JsonContractError(f"{description} contains non-explanation trailing text")
        try:
            _, suffix_end = json.JSONDecoder().raw_decode(suffix)
        except json.JSONDecodeError:
            pass
        else:
            if not suffix[suffix_end:].strip():
                raise JsonContractError(
                    f"{description} contains a second JSON value after the first"
                )
    return value
