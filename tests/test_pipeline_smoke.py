from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from api.common import ApiResponse
from api.seed_audio import GeneratedAudio
from api.seedance import SeedanceTask
from config import AppConfig
from core.models import JobSpec, UploadedAsset
from core.pipeline import VideoLocalizationPipeline
from media.ffprobe import MediaInfo
from utils.errors import PreflightError, ValidationError


def _spec(source: Path) -> JobSpec:
    return JobSpec(
        input_video=source,
        target_language="ar",
        target_region="Gulf",
        target_locale="ar-SA",
    )


class FakeArk:
    def __init__(self) -> None:
        self.calls: list[tuple[list[dict], dict]] = []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        value = {
            "source_language": "en",
            "target_language": "ar",
            "speakers": [{"id": "speaker_1", "visual_hint": "left person"}],
            "dialogues": [
                {
                    "speaker_id": "speaker_1",
                    "start_ms": 200,
                    "end_ms": 1200,
                    "source_text": "Hello",
                    "target_text": "مرحبا",
                }
            ],
        }
        return ApiResponse(
            {"choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}]},
            "ark-req",
        )

    @staticmethod
    def extract_text(response):
        return response.data["choices"][0]["message"]["content"]


class FakeSeedAudio:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Path, dict]] = []

    def generate_localized_audio(self, prompt, reference_audio_url, output_path, **kwargs):
        self.calls.append((prompt, reference_audio_url, Path(output_path), kwargs))
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"localized-wav")
        return GeneratedAudio(output_path, "seed-audio-req", {"code": 0})


class FakeUguu:
    def __init__(self) -> None:
        self.assets: list[UploadedAsset] = []

    def upload(self, path, *, kind="reference", **kwargs):
        asset = UploadedAsset(
            local_path=Path(path),
            remote_url=f"https://example.test/{Path(path).name}",
            uploaded_at=datetime.now(timezone.utc).isoformat(),
            kind=kind,
        )
        self.assets.append(asset)
        return asset

    def upload_many(self, items, **kwargs):
        return [self.upload(path, kind=kind, **kwargs) for path, kind in items]

    def check_access(self, **kwargs):
        return None


class FakeSeedance:
    def __init__(self) -> None:
        self.create_calls: list[tuple[list[dict], dict]] = []

    def create_task(self, content, **kwargs):
        self.create_calls.append((content, kwargs))
        return SeedanceTask("seedance-task", "seedance-req", {"id": "seedance-task"})

    def wait_task(self, task_id, **kwargs):
        return ApiResponse(
            {
                "id": task_id,
                "status": "succeeded",
                "content": {"video_url": "https://example.test/video.mp4"},
            },
            "seedance-query",
        )

    def check_access(self, **kwargs):
        return None


class FakeNoopArk:
    def __init__(self) -> None:
        self.calls = 0

    def chat(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("Ark analysis must not run after failed preflight")


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
                seedance_model_id="ep-test",
            )
            ark = FakeNoopArk()
            pipeline = VideoLocalizationPipeline(
                config,
                ark_client=ark,
                seed_audio_client=FakeSeedAudio(),
                uguu_client=FakeUguu(),
                seedance_client=FakeSeedance(),
            )
            with patch("core.pipeline.ffprobe.probe") as probe:
                with self.assertRaises(PreflightError):
                    pipeline.run(_spec(source))
                probe.assert_not_called()
            self.assertEqual(ark.calls, 0)

    def test_pipeline_uses_only_v2_stages_and_muxes_localized_audio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.mp4"
            source.write_bytes(b"source")
            config = AppConfig(work_dir=root / "work", seedance_model_id="ep-test")
            events: list[dict] = []
            ark = FakeArk()
            seed_audio = FakeSeedAudio()
            uguu = FakeUguu()
            seedance = FakeSeedance()
            pipeline = VideoLocalizationPipeline(
                config,
                ark_client=ark,
                seed_audio_client=seed_audio,
                uguu_client=uguu,
                seedance_client=seedance,
                event_callback=events.append,
            )
            mux_calls: list[tuple[Path, Path, Path]] = []

            def fake_extract_audio(video_path, output_path, **kwargs):
                output_path = Path(output_path)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"original-wav")
                return output_path

            def fake_download(url, output_path, **kwargs):
                output_path = Path(output_path)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"seedance-video")
                return output_path

            def fake_probe(path, **kwargs):
                path = Path(path)
                streams = (
                    [{"codec_type": "audio"}]
                    if path.suffix.lower() == ".wav"
                    else [{"codec_type": "video"}, {"codec_type": "audio"}]
                )
                return MediaInfo(
                    path=path,
                    duration=2.0,
                    streams=streams,
                    format_name="wav" if path.suffix.lower() == ".wav" else "mp4",
                    raw={
                        "format": {"duration": "2.0", "format_name": "test"},
                        "streams": streams,
                    },
                )

            def fake_mux(video_path, localized_audio_path, output_path, **kwargs):
                video_path = Path(video_path)
                localized_audio_path = Path(localized_audio_path)
                output_path = Path(output_path)
                mux_calls.append((video_path, localized_audio_path, output_path))
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"final-video")
                return output_path

            with patch("core.pipeline.download", side_effect=fake_download), patch(
                "core.pipeline.ffprobe.probe", side_effect=fake_probe
            ), patch("core.pipeline.ffmpeg.extract_audio", side_effect=fake_extract_audio), patch(
                "core.pipeline.ffmpeg.mux_video", side_effect=fake_mux
            ):
                output = pipeline.run(_spec(source), skip_preflight=True)

            self.assertTrue(output.is_file())
            self.assertEqual(output.name, "final_ar-SA.mp4")
            stage_events = [
                event["stage"]
                for event in events
                if event.get("event_type") == "stage"
            ]
            self.assertEqual(
                stage_events,
                ["analyzing", "generating_audio", "generating_video", "muxing"],
            )
            self.assertEqual(
                events[-1]["event_type"],
                "completed",
            )
            self.assertEqual(len(ark.calls), 1)
            self.assertEqual(ark.calls[0][1]["response_format"], {"type": "json_object"})
            self.assertEqual(len(seed_audio.calls), 1)
            self.assertEqual(seed_audio.calls[0][1], "https://example.test/original_audio.wav")
            self.assertEqual(len(seedance.create_calls), 1)
            content = seedance.create_calls[0][0]
            self.assertEqual(
                next(item for item in content if item["type"] == "audio_url")["audio_url"]["url"],
                "https://example.test/localized_audio.wav",
            )
            self.assertEqual(mux_calls[0][1], seed_audio.calls[0][2])

            job_id = next(event["job_id"] for event in events if event.get("job_id"))
            checkpoint = json.loads(
                (config.work_dir / job_id / "checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertEqual(checkpoint["pipeline_version"], 2)
            self.assertEqual(checkpoint["stage"], "completed")
            self.assertEqual(checkpoint["metrics"]["speaker_count"], 1)
            self.assertEqual(checkpoint["metrics"]["dialogue_count"], 1)
            self.assertEqual(checkpoint["cache_key"]["target_locale"], "ar-SA")
            log_lines = [
                json.loads(line)
                for line in (config.work_dir / job_id / "job.log").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(any(line.get("target_locale") == "ar-SA" for line in log_lines))
            self.assertTrue(any("stage_duration_seconds" in line for line in log_lines))

    def test_old_checkpoint_is_rejected_instead_of_falling_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.mp4"
            source.write_bytes(b"source")
            config = AppConfig(work_dir=root / "work")
            legacy_dir = config.work_dir / "legacy-job"
            legacy_dir.mkdir(parents=True)
            (legacy_dir / "checkpoint.json").write_text(
                json.dumps({"pipeline_version": 1, "stage": "ASR"}),
                encoding="utf-8",
            )
            pipeline = VideoLocalizationPipeline(config)
            with self.assertRaisesRegex(ValidationError, "start a new job"):
                pipeline._prepare_context(_spec(source), job_id="legacy-job")
