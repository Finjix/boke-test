from __future__ import annotations

import unittest

from api.common import ApiResponse
from api.minimax import MiniMaxClient, task_prompt, task_video_url
from config import AppConfig
from core.h3_prompt import build_context_ir_content
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

    def post(self, url: str, **kwargs):
        self.post_calls.append((url, kwargs))
        return FakeResponse(self.post_payload)

    def get(self, url: str, **kwargs):
        self.get_calls.append(url)
        return FakeResponse(self.get_payloads.pop(0))


class FailingPostSession(FakeSession):
    def post(self, url: str, **kwargs):
        self.post_calls.append((url, kwargs))
        raise __import__("requests").exceptions.RequestException("offline")


class MiniMaxAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AppConfig(
            minimax_api_key="test-key",
            poll_interval=0,
        )
        self.content = build_context_ir_content(
            "data:video/mp4;base64,AAAA",
            "localized requirement",
        )

    def test_context_ir_create_uses_multimodal_endpoint(self) -> None:
        session = FakeSession({"task_id": "ir-1"}, [])
        client = MiniMaxClient(self.config, session=session)

        task = client.create_context_ir_task(
            self.content,
            duration=5,
            ratio="adaptive",
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

        client.create_video_task(
            self.content,
            duration=5,
            resolution="768P",
            ratio="adaptive",
        )

        url, kwargs = session.post_calls[0]
        self.assertEqual(url, "https://api.minimax.cn/v2/video_generation")
        self.assertEqual(kwargs["json"]["model"], "MiniMax-H3")
        self.assertEqual(kwargs["json"]["resolution"], "768P")
        self.assertEqual(kwargs["json"]["content"], self.content)

    def test_wait_extracts_prompt_and_video_url(self) -> None:
        session = FakeSession(
            {"task_id": "ir-1"},
            [
                {
                    "task_id": "ir-1",
                    "status": "succeeded",
                    "content": {"prompt": "enhanced"},
                },
                {
                    "task_id": "h3-1",
                    "status": "succeeded",
                    "content": {"url": "https://cdn.example/result.mp4"},
                },
            ],
        )
        client = MiniMaxClient(self.config, session=session)

        ir = client.wait_task("ir-1", task_kind="H3-Context-IR")
        h3 = client.wait_task("h3-1", task_kind="MiniMax-H3")

        self.assertEqual(task_prompt(ir.data), "enhanced")
        self.assertEqual(task_video_url(h3.data), "https://cdn.example/result.mp4")

    def test_failed_status_and_missing_task_id_are_terminal(self) -> None:
        failed = FakeSession(
            {"task_id": "ir-1"},
            [{"task_id": "ir-1", "status": "failed"}],
        )
        with self.assertRaises(ProviderError):
            MiniMaxClient(self.config, session=failed).wait_task(
                "ir-1",
                task_kind="H3-Context-IR",
            )

        missing = FakeSession({}, [])
        client = MiniMaxClient(self.config, session=missing)
        with self.assertRaises(ProviderError):
            client.create_context_ir_task(self.content, duration=5)
        self.assertEqual(len(missing.post_calls), 1)

    def test_network_create_error_is_not_retried(self) -> None:
        session = FailingPostSession({}, [])
        with self.assertRaises(ProviderError):
            MiniMaxClient(self.config, session=session).create_context_ir_task(
                self.content,
                duration=5,
            )
        self.assertEqual(len(session.post_calls), 1)


if __name__ == "__main__":
    unittest.main()
