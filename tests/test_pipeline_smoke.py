from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api.common import ApiResponse
from api.mediakit import MediaKitTask
from api.seed_audio import GeneratedAudio
from api.seedance import SeedanceTask
from config import AppConfig
from core.models import JobSpec, UploadedAsset
from core.pipeline import VideoLocalizationPipeline
from media.ffprobe import MediaInfo
from utils.errors import PreflightError


class FakeMediaKit:
    def separate_voice(self, *args, **kwargs):
        return MediaKitTask(
            "media-separate",
            "media-req",
            {
                "result": {
                    "voice_audio_url": "https://example.test/voice.wav",
                    "background_audio_url": "https://example.test/background.wav",
                }
            },
        )

    def asr(self, *args, **kwargs):
        return MediaKitTask(
            "media-asr",
            "asr-req",
            {
                "result": {
                    "subtitles": [
                        {
                            "start_time": 0.1,
                            "end_time": 1.0,
                            "subtitle_text": "Hello",
                            "speaker": "speaker_0",
                        }
                    ]
                }
            },
        )


class FakeArk:
    def chat(self, messages, **kwargs):
        text = messages[-1]["content"]
        if isinstance(text, list):
            value = '{"speaker_id":"speaker_0","gender":"male","age_group":"young","role_type":"main_character","voice_style":["calm"],"confidence":0.8}'
        else:
            value = '[{"id":"seg_0001","speaker":"speaker_0","start":0.1,"end":1.0,"text":"مرحبا"}]'
        return ApiResponse({"choices": [{"message": {"content": value}}]}, "ark-req")

    @staticmethod
    def extract_text(response):
        return response.data["choices"][0]["message"]["content"]


class FakeSeedAudio:
    def generate_dialogue(self, prompt, output_path, **kwargs):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"audio")
        return GeneratedAudio(output_path, "seed-req", {"code": 0})


class FakeUguu:
    def upload(self, path, *, kind="reference", **kwargs):
        return UploadedAsset(
            local_path=Path(path),
            remote_url=f"https://example.test/{Path(path).name}",
            uploaded_at="now",
            kind=kind,
        )

    def upload_many(self, items, **kwargs):
        return [self.upload(path, kind=kind) for path, kind in items]


class FakeSeedance:
    def create_task(self, content, **kwargs):
        return SeedanceTask("seedance-task", "seedance-req", {"id": "seedance-task"})

    def wait_task(self, task_id, **kwargs):
        return ApiResponse(
            {"id": task_id, "status": "succeeded", "content": {"video_url": "https://example.test/video.mp4"}},
            "seedance-query",
        )


class NoNetworkProvider:
    def check_access(self, **kwargs):
        return None

    def doctor(self, **kwargs):
        return "ok"

    def schema(self, *args, **kwargs):
        return {"ok": True}


class PipelineSmokeTests(unittest.TestCase):
    def test_preflight_failure_stops_before_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.mp4"
            source.write_bytes(b"source")
            config = AppConfig(
                work_dir=root / "work",
                ffmpeg_bin="missing-ffmpeg",
                ffprobe_bin="missing-ffprobe",
                mediakit_cli_bin="missing-mediakit",
            )
            pipeline = VideoLocalizationPipeline(
                config,
                media_client=NoNetworkProvider(),
                ark_client=NoNetworkProvider(),
                seed_audio_client=NoNetworkProvider(),
                uguu_client=NoNetworkProvider(),
                seedance_client=NoNetworkProvider(),
            )
            with patch("core.pipeline.ffprobe.probe") as probe:
                with self.assertRaises(PreflightError):
                    pipeline.run(
                        JobSpec(
                            input_video=source,
                            target_language="Arabic",
                            target_region="Gulf",
                        )
                    )
                probe.assert_not_called()

    def test_pipeline_writes_final_artifact_with_fake_external_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.mp4"
            source.write_bytes(b"source")
            config = AppConfig(work_dir=root / "work", seedance_model_id="ep-test")
            events: list[dict] = []
            pipeline = VideoLocalizationPipeline(
                config,
                media_client=FakeMediaKit(),
                ark_client=FakeArk(),
                seed_audio_client=FakeSeedAudio(),
                uguu_client=FakeUguu(),
                seedance_client=FakeSeedance(),
                event_callback=events.append,
            )

            def fake_download(url, output_path, **kwargs):
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"downloaded")
                return output_path

            def fake_probe(path, **kwargs):
                return MediaInfo(
                    path=Path(path),
                    duration=2.0,
                    streams=[{"codec_type": "video"}, {"codec_type": "audio"}],
                    format_name="test",
                    raw={"format": {"duration": "2.0"}, "streams": []},
                )

            def fake_frame_extract(*args, **kwargs):
                return {"speaker_0": [root / "frame.jpg"]}

            def fake_media_output(_a, _b, output_path, **kwargs):
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"media")
                return output_path

            with patch("core.pipeline.download", side_effect=fake_download), patch(
                "core.pipeline.ffprobe.probe", side_effect=fake_probe
            ), patch("core.pipeline.extract_anchor_frames", side_effect=fake_frame_extract), patch(
                "core.pipeline.ffmpeg.mix_audio", side_effect=fake_media_output
            ), patch("core.pipeline.ffmpeg.mux_video", side_effect=fake_media_output):
                output = pipeline.run(
                    JobSpec(
                        input_video=source,
                        target_language="Arabic",
                        target_region="Gulf",
                    ),
                    skip_preflight=True,
                )
            self.assertTrue(output.is_file())
            self.assertTrue(any(event.get("event_type") == "completed" for event in events))
            job_id = next(event["job_id"] for event in events if event.get("job_id"))
            context, state = pipeline._prepare_context(
                JobSpec(
                    input_video=source,
                    target_language="Arabic",
                    target_region="Gulf",
                ),
                job_id=job_id,
            )
            self.assertEqual(context.job_id, job_id)
            self.assertAlmostEqual(state["source_duration"], 2.0)
