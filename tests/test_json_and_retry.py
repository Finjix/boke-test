from __future__ import annotations

import unittest

import utils.json_parser as json_parser
from h3_workflow import build_parser
from utils.errors import JsonContractError, ProviderError
from utils.json_parser import parse_strict_json
from utils.retry import retry_call


class JsonAndRetryTests(unittest.TestCase):
    def test_strict_json_rejects_markdown_and_trailing_text(self) -> None:
        with self.assertRaises(JsonContractError):
            parse_strict_json("```json\n{}\n```")
        with self.assertRaises(JsonContractError):
            parse_strict_json("{} explanation")

    def test_doubao_mode_accepts_only_plain_explanation_after_one_json_value(self) -> None:
        self.assertEqual(
            parse_strict_json(
                '{"ok": true}\nThis is an explanation.',
                allow_trailing_explanation=True,
            ),
            {"ok": True},
        )
        with self.assertRaises(JsonContractError):
            parse_strict_json(
                '{} {"second": true}',
                allow_trailing_explanation=True,
            )
        with self.assertRaises(JsonContractError):
            parse_strict_json(
                "{}\n```text",
                allow_trailing_explanation=True,
            )

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

    def test_cli_exposes_explicit_doubao_approval_retry_and_auto_continue(self) -> None:
        parser = build_parser()
        self.assertTrue(
            parser.parse_args(["start", "--video", "input.mp4", "--auto-continue"]).auto_continue
        )
        start = parser.parse_args(
            ["start", "--video", "input.mp4", "--auto-continue-doubao"]
        )
        self.assertTrue(start.auto_continue_doubao)
        self.assertEqual(
            parser.parse_args(["approve-doubao", "--job-id", "job-1"]).command,
            "approve-doubao",
        )
        self.assertEqual(
            parser.parse_args(["retry-doubao", "--job-id", "job-1"]).command,
            "retry-doubao",
        )
        self.assertEqual(
            parser.parse_args(["approve-seedream", "--job-id", "job-1"]).command,
            "approve-seedream",
        )
        retry_reference = parser.parse_args(
            ["retry-seedream", "--job-id", "job-1", "--shot-id", "shot_001"]
        )
        self.assertEqual(retry_reference.command, "retry-seedream")
        self.assertEqual(retry_reference.shot_id, "shot_001")
        refreshed = parser.parse_args(
            ["retry", "--job-id", "job-1", "--segment", "1", "--refresh-prompt"]
        )
        self.assertTrue(refreshed.refresh_prompt)

