from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from api.ark import ArkClient
from api.common import ApiResponse
from api.minimax import MiniMaxClient, task_video_url
from api.seedance import SeedanceClient
from api.uguu import UguuClient
from config import (
    AppConfig,
    FIXED_DOUBAO_MODEL,
    FIXED_MINIMAX_H3_MODEL,
    FIXED_SEEDREAM_MODEL,
)
from core.localization import analyze_video
from utils.errors import ProviderError


class FakeResponse:
    def __init__(self, data: dict, status_code: int = 200, content: bytes = b""):
        self._data = data
        self.status_code = status_code
        self.content = content
        self.text = json.dumps(data)

    def json(self) -> dict:
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = responses
        self.posts: list[dict] = []
        self.gets: list[dict] = []

    def post(self, *args, **kwargs):
        self.posts.append({"args": args, **kwargs})
        return self.responses.pop(0)

    def get(self, *args, **kwargs):
        self.gets.append({"args": args, **kwargs})
        return self.responses.pop(0)


def _valid_package() -> dict:
    return {
        "source": {"language": "en"},
        "target": {"language": "ar", "region": "Gulf", "locale": "ar-SA"},
        "video_analysis": {"theme": "conversation"},
        "speakers": [{"id": "speaker_1", "visual_hint": "off-screen narrator"}],
        "dialogues": [
            {
                "speaker_id": "speaker_1",
                "start_ms": 0,
                "end_ms": 1000,
                "source_text": "Hello",
                "target_text": "مرحبا",
            }
        ],
        "visual_localization": {"characters": "Gulf narrator"},
        "cultural_requirements": [],
    }


class ProviderAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AppConfig(
            ark_api_key="ark-key",
            seedance_model_id="ep-test",
            max_retries=1,
        )

    def test_ark_sends_video_multimodal_input_fixed_model_and_json_mode(self) -> None:
        session = FakeSession(
            [FakeResponse({"choices": [{"message": {"content": "{\"ok\":true}"}}]})]
        )
        client = ArkClient(self.config, session=session)
        response = client.chat(
            [
                {"role": "system", "content": "return JSON"},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "video_url",
                            "video_url": {"url": "https://uguu.se/input.mp4"},
                        },
                        {"type": "text", "text": "analyze"},
                    ],
                },
            ],
            response_format={"type": "json_object"},
        )
        payload = session.posts[0]["json"]
        self.assertEqual(payload["model"], FIXED_DOUBAO_MODEL)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["messages"][1]["content"][0]["type"], "video_url")
        self.assertEqual(ArkClient.extract_text(response), "{\"ok\":true}")

    def test_analysis_retries_same_model_once_with_validation_error(self) -> None:
        class FakeArk:
            def __init__(self) -> None:
                self.calls: list[tuple[list[dict], dict]] = []

            def chat(self, messages, **kwargs):
                self.calls.append((messages, kwargs))
                content = "not json" if len(self.calls) == 1 else json.dumps(
                    _valid_package(), ensure_ascii=False
                )
                return ApiResponse(
                    {"choices": [{"message": {"content": content}}]},
                    f"req-{len(self.calls)}",
                )

            @staticmethod
            def extract_text(response):
                return response.data["choices"][0]["message"]["content"]

        client = FakeArk()
        result = analyze_video(
            client,
            "https://uguu.se/input.mp4",
            target_language="ar",
            target_region="Gulf",
            target_locale="ar-SA",
            duration_seconds=2,
        )
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(
            client.calls[0][1]["response_format"],
            {"type": "json_object"},
        )
        self.assertIn("Validation error", client.calls[1][0][1]["content"][1]["text"])
        self.assertEqual(result.dialogues[0].target_text, "مرحبا")

    def test_seedream_uses_fixed_image_edit_model_and_persists_raw_response(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "data": [{"url": "https://cdn.example/seedream.png"}],
                        "request_id": "seedream-provider-request",
                    }
                )
            ]
        )
        client = ArkClient(self.config, session=session)
        with tempfile.TemporaryDirectory() as directory:
            response = client.generate_image(
                ["https://uguu.se/source-frame.png"],
                "Replace the complete background while preserving composition.",
                stage="seedream_shot_001_attempt_1",
                raw_dir=Path(directory),
            )
            raw_files = list(Path(directory).glob("*.json"))

        payload = session.posts[0]["json"]
        self.assertEqual(payload["model"], FIXED_SEEDREAM_MODEL)
        self.assertEqual(payload["image"], "https://uguu.se/source-frame.png")
        self.assertEqual(payload["size"], "2K")
        self.assertFalse(payload["watermark"])
        self.assertEqual(response.request_id, "seedream-provider-request")
        self.assertEqual(len(raw_files), 1)

    def test_seedream_serializes_previous_reference_as_second_input(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "data": [{"url": "https://cdn.example/seedream-continuity.png"}],
                        "request_id": "seedream-continuity-request",
                    }
                )
            ]
        )
        client = ArkClient(self.config, session=session)
        client.generate_image(
            [
                "https://uguu.se/current-keyframe.png",
                "https://uguu.se/previous-seedream.png",
            ],
            "Keep the current shot composition and use the previous image for continuity.",
        )

        payload = session.posts[0]["json"]
        self.assertEqual(
            payload["image"],
            [
                "https://uguu.se/current-keyframe.png",
                "https://uguu.se/previous-seedream.png",
            ],
        )
        self.assertEqual(payload["model"], FIXED_SEEDREAM_MODEL)

    def test_seedance_enables_native_audio_generation(self) -> None:
        session = FakeSession([FakeResponse({"id": "task-1"})])
        client = SeedanceClient(self.config, session=session)
        task = client.create_task(
            [
                {"type": "text", "text": "localized video with natural dialogue"},
                {"type": "video_url", "video_url": {"url": "https://uguu.se/video.mp4"}},
            ]
        )
        payload = session.posts[0]["json"]
        self.assertEqual(task.task_id, "task-1")
        self.assertEqual(payload["model"], "ep-test")
        self.assertTrue(payload["generate_audio"])

    def test_seedance_poll_timeout_is_bounded(self) -> None:
        class AlwaysQueuedSession:
            def get(self, *args, **kwargs):
                return FakeResponse({"id": "task-1", "status": "queued"})

        client = SeedanceClient(
            self.config,
            session=AlwaysQueuedSession(),
            sleeper=lambda _seconds: None,
        )
        with self.assertRaises(ProviderError) as raised:
            client.wait_task("task-1", max_wait_seconds=0.001)
        self.assertEqual(raised.exception.error_code, "TASK_TIMEOUT")

    def test_minimax_h3_uses_domestic_reference_payload_without_adaptive_ratio(self) -> None:
        session = FakeSession([FakeResponse({"task_id": "h3-task-1", "request_id": "h3-request-1"})])
        config = self.config.with_overrides(minimax_api_key="minimax-key")
        client = MiniMaxClient(config, session=session)
        task = client.create_task(
            [
                {"type": "text", "text": "transform the source"},
                {
                    "type": "video_url",
                    "video_url": {"url": "https://uguu.se/source.mp4"},
                    "role": "reference_video",
                },
            ],
            duration=8,
            resolution="2K",
        )
        payload = session.posts[0]["json"]
        self.assertEqual(session.posts[0]["args"][0], "https://api.minimax.cn/v2/video_generation")
        self.assertEqual(task.task_id, "h3-task-1")
        self.assertEqual(payload["model"], FIXED_MINIMAX_H3_MODEL)
        self.assertEqual(payload["duration"], 8)
        self.assertEqual(payload["resolution"], "2K")
        self.assertNotIn("ratio", payload)

    def test_minimax_h3_query_returns_content_url(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "task": {
                            "task_id": "h3-task-1",
                            "status": "succeeded",
                            "content": {"url": "https://cdn.example/h3.mp4"},
                        }
                    }
                )
            ]
        )
        config = self.config.with_overrides(minimax_api_key="minimax-key")
        client = MiniMaxClient(config, session=session, sleeper=lambda _seconds: None)
        response = client.wait_task("h3-task-1", max_wait_seconds=1)
        self.assertEqual(task_video_url(response.data), "https://cdn.example/h3.mp4")
        self.assertEqual(
            session.gets[0]["args"][0],
            "https://api.minimax.cn/v2/query/video_generation/h3-task-1",
        )

    def test_ark_auth_error_is_not_retried(self) -> None:
        session = FakeSession([FakeResponse({"error": {"code": "Unauthorized"}}, status_code=401)])
        client = ArkClient(self.config, session=session)
        with self.assertRaises(ProviderError) as raised:
            client.chat([], raw_dir=None)
        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(len(session.posts), 1)

    def test_uguu_upload_uses_files_array_and_validates_https_result(self) -> None:
        session = FakeSession(
            [FakeResponse({"success": True, "files": [{"url": "https://uguu.se/file.mp4"}]})]
        )
        client = UguuClient(self.config, session=session)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.mp4"
            source.write_bytes(b"input")
            asset = client.upload(source, kind="source_video")
        self.assertEqual(asset.remote_url, "https://uguu.se/file.mp4")
        self.assertIn("files[]", session.posts[0]["files"])
