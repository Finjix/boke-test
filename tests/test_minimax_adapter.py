from __future__ import annotations

import unittest

import requests

from api.minimax import MiniMaxClient, task_prompt, task_video_url
from config import AppConfig
from core.h3_prompt import build_context_ir_content, build_video_content
from utils.errors import ProviderError


class FakeResponse:
    def __init__(self, payload: object, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self) -> object:
        return self.payload


class FakeSession:
    trust_env = True

    def __init__(self, post_payload: object, get_payloads: list[object]):
        self.post_payload = post_payload
        self.get_payloads = list(get_payloads)
        self.post_calls: list[tuple[str, dict[str, object]]] = []
        self.get_calls: list[str] = []

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.post_calls.append((url, kwargs))
        return FakeResponse(self.post_payload)

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.get_calls.append(url)
        return FakeResponse(self.get_payloads.pop(0))


class FailingPostSession(FakeSession):
    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.post_calls.append((url, kwargs))
        raise requests.exceptions.ConnectionError("offline")


class MiniMaxAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AppConfig(minimax_api_key="test-key", poll_interval=0)
        self.content = build_context_ir_content(
            "https://uguu.example/source.mp4", "localized requirement"
        )

    def test_context_ir_create_uses_dedicated_endpoint_and_payload(self) -> None:
        session = FakeSession({"task_id": "ir-1", "request_id": "req-ir"}, [])
        client = MiniMaxClient(self.config, session=session)

        task = client.create_context_ir_task(
            self.content, duration=5, ratio="adaptive"
        )

        url, kwargs = session.post_calls[0]
        self.assertEqual(url, "https://api.minimax.cn/v2/h3_context_ir")
        self.assertEqual(
            kwargs["json"],
            {
                "model": "MiniMax-H3",
                "content": self.content,
                "duration": 5,
                "ratio": "adaptive",
            },
        )
        self.assertEqual(task.task_id, "ir-1")

    def test_video_create_uses_h3_endpoint_and_resolution(self) -> None:
        session = FakeSession({"task_id": "h3-1"}, [])
        client = MiniMaxClient(self.config, session=session)
        content = build_video_content("https://uguu.example/source.mp4", "IR prompt")

        client.create_video_task(content, duration=5, resolution="768P")

        url, kwargs = session.post_calls[0]
        self.assertEqual(url, "https://api.minimax.cn/v2/video_generation")
        self.assertEqual(
            kwargs["json"],
            {
                "model": "MiniMax-H3",
                "content": content,
                "duration": 5,
                "resolution": "768P",
                "ratio": "adaptive",
            },
        )

    def test_wait_extracts_content_prompt_and_content_url(self) -> None:
        session = FakeSession(
            {"task_id": "ir-1"},
            [
                {
                    "task_id": "ir-1",
                    "status": "succeeded",
                    "content": {"prompt": "  preserve this prompt  "},
                },
                {
                    "task_id": "h3-1",
                    "status": "succeeded",
                    "content": {"url": "https://cdn.example/result.mp4"},
                },
            ],
        )
        client = MiniMaxClient(self.config, session=session)

        ir = client.wait_task("ir-1", task_kind="minimax_context_ir")
        h3 = client.wait_task("h3-1", task_kind="minimax_h3")

        self.assertEqual(task_prompt(ir.data), "  preserve this prompt  ")
        self.assertEqual(task_video_url(h3.data), "https://cdn.example/result.mp4")
        self.assertEqual(
            session.get_calls,
            [
                "https://api.minimax.cn/v2/query/video_generation/ir-1",
                "https://api.minimax.cn/v2/query/video_generation/h3-1",
            ],
        )

    def test_result_fields_are_strict_and_failed_status_terminates(self) -> None:
        self.assertIsNone(task_prompt({"task": {"prompt": "wrong location"}}))
        self.assertIsNone(task_video_url({"task": {"url": "wrong location"}}))
        session = FakeSession(
            {"task_id": "ir-1"},
            [{"task_id": "ir-1", "status": "failed"}],
        )
        client = MiniMaxClient(self.config, session=session)

        with self.assertRaises(ProviderError):
            client.wait_task("ir-1", task_kind="minimax_context_ir")

        missing_prompt_session = FakeSession(
            {"task_id": "ir-2"},
            [{"task_id": "ir-2", "status": "succeeded", "content": {}}],
        )
        missing_prompt_client = MiniMaxClient(
            self.config, session=missing_prompt_session
        )
        with self.assertRaises(ProviderError):
            missing_prompt_client.wait_task("ir-2", task_kind="minimax_context_ir")

    def test_missing_create_task_id_fails_without_second_create(self) -> None:
        session = FakeSession({}, [])
        client = MiniMaxClient(self.config, session=session)

        with self.assertRaises(ProviderError):
            client.create_context_ir_task(self.content, duration=5)
        self.assertEqual(len(session.post_calls), 1)

    def test_create_network_error_is_terminal_without_retry(self) -> None:
        session = FailingPostSession({}, [])
        client = MiniMaxClient(self.config, session=session)

        with self.assertRaises(ProviderError):
            client.create_context_ir_task(self.content, duration=5)
        self.assertEqual(len(session.post_calls), 1)


if __name__ == "__main__":
    unittest.main()
