from __future__ import annotations

import unittest

import utils.json_parser as json_parser
from utils.errors import JsonContractError, ProviderError
from utils.json_parser import parse_strict_json
from utils.retry import retry_call


class JsonAndRetryTests(unittest.TestCase):
    def test_strict_json_rejects_markdown_and_trailing_text(self) -> None:
        with self.assertRaises(JsonContractError):
            parse_strict_json("```json\n{}\n```")
        with self.assertRaises(JsonContractError):
            parse_strict_json("{} explanation")

    def test_legacy_cli_json_parser_was_removed(self) -> None:
        self.assertFalse(hasattr(json_parser, "parse_cli_json"))

    def test_retry_uses_bounded_backoff_without_sleeping_in_test(self) -> None:
        calls = 0
        delays: list[float] = []

        def operation() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ProviderError("busy", provider="test", status_code=503)
            return "ok"

        result = retry_call(operation, attempts=3, sleeper=delays.append)
        self.assertEqual(result, "ok")
        self.assertEqual(delays, [1.0, 2.0])

