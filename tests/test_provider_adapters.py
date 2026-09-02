from __future__ import annotations

import base64
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from api.ark import ArkClient
from api.common import ApiResponse
from api.seed_audio import SeedAudioClient
from api.seedance import SeedanceClient
from api.uguu import UguuClient
from config import AppConfig, FIXED_DOUBAO_MODEL, FIXED_SEED_AUDIO_MODEL
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


class ProviderAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AppConfig(
            ark_api_key="ark-key",
            seed_audio_api_key="audio-key",
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
        self.assertEqual(ArkClient.extract_text(response), '{"ok":true}')

    def test_analysis_retries_same_model_once_with_validation_error(self) -> None:
        valid = {
            "source_language": "en",
            "target_language": "ar",
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
        }

        class FakeArk:
            def __init__(self) -> None:
                self.calls: list[tuple[list[dict], dict]] = []

            def chat(self, messages, **kwargs):
                self.calls.append((messages, kwargs))
                content = "not json" if len(self.calls) == 1 else json.dumps(valid, ensure_ascii=False)
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

    def test_seed_audio_uses_complete_reference_audio_and_fixed_payload(self) -> None:
        audio = base64.b64encode(b"wav-bytes").decode()
        session = FakeSession([FakeResponse({"code": 0, "audio": audio})])
        client = SeedAudioClient(self.config, session=session)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "localized_audio.wav"
            result = client.generate_localized_audio(
                "Use @Audio1 and recreate the complete scene",
                "https://uguu.se/original_audio.wav",
                output,
            )
            self.assertEqual(output.read_bytes(), b"wav-bytes")
        payload = session.posts[0]["json"]
        self.assertEqual(payload["model"], FIXED_SEED_AUDIO_MODEL)
        self.assertEqual(
            payload["references"],
            [{"audio_url": "https://uguu.se/original_audio.wav"}],
        )
        self.assertEqual(result.request_id, session.posts[0]["headers"]["X-Api-Request-Id"])
        self.assertFalse(hasattr(client, "generate_dialogue"))

    def test_seed_audio_downloads_url_result(self) -> None:
        session = FakeSession(
            [
                FakeResponse({"code": 0, "url": "https://example.test/audio.wav"}),
                FakeResponse({}, content=b"url-audio"),
            ]
        )
        client = SeedAudioClient(self.config, session=session)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "localized_audio.wav"
            client.generate_localized_audio(
                "complete scene",
                "https://uguu.se/original.wav",
                output,
            )
            self.assertEqual(output.read_bytes(), b"url-audio")

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

    def test_seedance_disables_audio_generation(self) -> None:
        session = FakeSession([FakeResponse({"id": "task-1"})])
        client = SeedanceClient(self.config, session=session)
        task = client.create_task(
            [
                {"type": "video_url", "video_url": {"url": "https://uguu.se/video.mp4"}},
                {"type": "audio_url", "audio_url": {"url": "https://uguu.se/audio.wav"}},
            ]
        )
        payload = session.posts[0]["json"]
        self.assertEqual(task.task_id, "task-1")
        self.assertEqual(payload["model"], "ep-test")
        self.assertFalse(payload["generate_audio"])

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

    def test_ark_auth_error_is_not_retried(self) -> None:
        session = FakeSession([FakeResponse({"error": {"code": "Unauthorized"}}, status_code=401)])
        client = ArkClient(self.config, session=session)
        with self.assertRaises(ProviderError) as raised:
            client.chat([], raw_dir=None)
        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(len(session.posts), 1)
