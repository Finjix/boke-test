from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api.ark import ArkClient
from api.common import ApiResponse
from api.mediakit import MediaKitClient
from api.seed_audio import SeedAudioClient
from api.seedance import SeedanceClient
from api.uguu import UguuClient
from config import AppConfig
from core.translator import translate_segments
from core.timeline import normalize_asr
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

    def post(self, *args, **kwargs):
        self.posts.append(kwargs)
        return self.responses.pop(0)

    def get(self, *args, **kwargs):
        return self.responses.pop(0)


class ProviderAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AppConfig(
            ark_api_key="ark-key",
            mediakit_api_key="media-key",
            seed_audio_api_key="audio-key",
            seedance_model_id="ep-test",
            max_retries=1,
        )

    def test_seed_audio_decodes_base64_and_keeps_fixed_payload(self) -> None:
        audio = base64.b64encode(b"wav-bytes").decode()
        session = FakeSession([FakeResponse({"code": 0, "audio": audio, "original_duration": 2.0})])
        client = SeedAudioClient(self.config, session=session)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "voice.wav"
            result = client.generate_dialogue("DRY DIALOGUE ONLY", output)
            self.assertEqual(output.read_bytes(), b"wav-bytes")
            payload = session.posts[0]["json"]
            self.assertEqual(payload["model"], "seed-audio-1.0")
            self.assertNotIn("references", payload)
            self.assertEqual(result.original_duration, 2.0)

    def test_seed_audio_downloads_url_result(self) -> None:
        session = FakeSession([
            FakeResponse({"code": 0, "url": "https://example.test/audio.wav"}),
            FakeResponse({}, content=b"url-audio"),
        ])
        client = SeedAudioClient(self.config, session=session)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "voice.wav"
            client.generate_dialogue("DRY DIALOGUE ONLY", output)
            self.assertEqual(output.read_bytes(), b"url-audio")

    def test_ark_extracts_response_text(self) -> None:
        response = ApiResponse(
            data={"choices": [{"message": {"content": "{\"ok\":true}"}}]},
            request_id="req",
        )
        self.assertEqual(ArkClient.extract_text(response), '{"ok":true}')

    def test_translation_retries_same_client_on_contract_error(self) -> None:
        segments = normalize_asr(
            {
                "result": {
                    "subtitles": [
                        {
                            "start_time": 0,
                            "end_time": 1,
                            "subtitle_text": "Hello",
                            "speaker": "speaker_0",
                        }
                    ]
                }
            }
        )

        class FakeArk:
            def __init__(self):
                self.calls = 0

            def chat(self, messages, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return ApiResponse(
                        {"choices": [{"message": {"content": "not json"}}]},
                        "req-1",
                    )
                return ApiResponse(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": '[{"id":"seg_0001","speaker":"speaker_0","start":0,"end":1,"text":"مرحبا"}]'
                                }
                            }
                        ]
                    },
                    "req-2",
                )

            @staticmethod
            def extract_text(response):
                return response.data["choices"][0]["message"]["content"]

        client = FakeArk()
        result = translate_segments(
            client,
            segments,
            target_language="Arabic",
            target_region="Gulf",
            validation_attempts=2,
        )
        self.assertEqual(client.calls, 2)
        self.assertEqual(result[0].text, "مرحبا")

    def test_mediakit_adapter_keeps_cli_as_only_entrypoint(self) -> None:
        client = MediaKitClient(self.config)
        outputs = [
            '{"task_id":"task_1","request_id":"req_1"}',
            '{"result":{"voice_audio_url":"https://x/voice.wav","background_audio_url":"https://x/bg.wav"}}',
        ]

        def fake_run(command, **kwargs):
            return __import__("subprocess").CompletedProcess(command, 0, stdout=outputs.pop(0), stderr="cli log")

        with tempfile.TemporaryDirectory() as directory:
            with patch("api.mediakit.subprocess.run", side_effect=fake_run) as run:
                result = client.separate_voice(Path(directory) / "input.mp4", raw_dir=Path(directory) / "raw")
        self.assertEqual(result.task_id, "task_1")
        self.assertEqual(result.result["voice_audio_url"], "https://x/voice.wav")
        self.assertEqual(run.call_count, 2)
        self.assertTrue(any("mediakit-cli" in str(call.args[0][0]) for call in run.call_args_list))

    def test_mediakit_asr_passes_explicit_source_language(self) -> None:
        client = MediaKitClient(self.config)
        outputs = [
            '{"task_id":"task_1","request_id":"req_1"}',
            '{"result":{"subtitles":[]}}',
        ]

        def fake_run(command, **kwargs):
            return __import__("subprocess").CompletedProcess(
                command,
                0,
                stdout=outputs.pop(0),
                stderr="cli log",
            )

        with tempfile.TemporaryDirectory() as directory:
            with patch("api.mediakit.subprocess.run", side_effect=fake_run) as run:
                client.asr(
                    Path(directory) / "voice.wav",
                    language="eng-US",
                    raw_dir=Path(directory) / "raw",
                )
        command = run.call_args_list[0].args[0]
        language_index = command.index("--language")
        self.assertEqual(command[language_index + 1], "eng-US")

    def test_mediakit_cli_timeout_is_internal_error(self) -> None:
        client = MediaKitClient(self.config)
        timeout = subprocess.TimeoutExpired(["mediakit-cli"], 1)
        with patch("api.mediakit.subprocess.run", side_effect=timeout):
            with self.assertRaises(ProviderError) as raised:
                client.schema("audio", "separate-voice")
        self.assertEqual(raised.exception.error_code, "CLI_TIMEOUT")
        self.assertTrue(raised.exception.retryable)

    def test_uguu_upload_uses_files_array_and_validates_https_result(self) -> None:
        session = FakeSession([
            FakeResponse({"success": True, "files": [{"url": "https://uguu.se/file.mp4"}]}),
        ])
        client = UguuClient(self.config, session=session)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.mp4"
            source.write_bytes(b"input")
            asset = client.upload(source, kind="source_video")
        self.assertEqual(asset.remote_url, "https://uguu.se/file.mp4")
        self.assertIn("files[]", session.posts[0]["files"])

    def test_seedance_create_and_poll_preserve_model_and_discard_policy(self) -> None:
        session = FakeSession([
            FakeResponse({"id": "task-1"}),
            FakeResponse({"id": "task-1", "status": "queued"}),
            FakeResponse({"id": "task-1", "status": "succeeded", "content": {"video_url": "https://x/video.mp4"}}),
        ])
        client = SeedanceClient(self.config, session=session, sleeper=lambda _seconds: None)
        task = client.create_task([{"type": "text", "text": "localize"}])
        result = client.wait_task(task.task_id)
        self.assertEqual(task.task_id, "task-1")
        self.assertEqual(result.data["status"], "succeeded")
        self.assertTrue(session.posts[0]["json"]["generate_audio"])
        self.assertEqual(session.posts[0]["json"]["model"], "ep-test")

    def test_seedance_poll_timeout_is_bounded(self) -> None:
        class AlwaysQueuedSession:
            def get(self, *args, **kwargs):
                return FakeResponse({"id": "task-1", "status": "queued"})

        session = AlwaysQueuedSession()
        client = SeedanceClient(self.config, session=session, sleeper=lambda _seconds: None)
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
