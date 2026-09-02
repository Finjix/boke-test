from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from api.common import ApiResponse
from api.seedance import SeedanceTask
from config import AppConfig
from core.models import JobSpec, UploadedAsset
from core.pipeline import PIPELINE_VERSION, VideoLocalizationPipeline
from media.ffprobe import MediaInfo
from utils.errors import PreflightError, ValidationError


def _spec(source: Path) -> JobSpec:
    return JobSpec(
        input_video=source,
        target_language="ar",
        target_region="Gulf",
        target_locale="ar-SA",
    )


def _package() -> dict:
    return {
        "source": {"language": "en"},
        "target": {"language": "ar", "region": "Gulf", "locale": "ar-SA"},
        "video_analysis": {
            "theme": "conversation",
            "story_structure": "setup and response",
            "shot_structure": "medium alternating shots",
            "scene_environment": "office",
            "character_relationships": "colleagues",
            "product_information": "none",
            "core_creative": "preserve the exchange",
        },
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
        "visual_localization": {
            "characters": "modern Gulf business person",
            "wardrobe": "contemporary regional wardrobe",
            "environment": "modern Gulf office",
            "architecture": "regional modern architecture",
            "props": "preserve all key props",
        },
        "cultural_requirements": ["Respectful and natural Gulf-market portrayal"],
    }


class FakeArk:
    def __init__(self) -> None:
        self.calls: list[tuple[list[dict], dict]] = []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return ApiResponse(
            {"choices": [{"message": {"content": json.dumps(_package(), ensure_ascii=False)}}]},
            "ark-req",
        )

    @staticmethod
    def extract_text(response):
        return response.data["choices"][0]["message"]["content"]


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
        self.wait_calls: list[str] = []

    def create_task(self, content, **kwargs):
        self.create_calls.append((content, kwargs))
        return SeedanceTask("seedance-task", "seedance-req", {"id": "seedance-task"})

    def wait_task(self, task_id, **kwargs):
        self.wait_calls.append(task_id)
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
    def _probe(self, path: Path, *, has_audio: bool = True) -> MediaInfo:
        streams = [{"codec_type": "video"}]
        if has_audio:
            streams.append({"codec_type": "audio"})
        return MediaInfo(
            path=path,
            duration=2.0,
            streams=streams,
            format_name="mp4",
            raw={
                "format": {"duration": "2.0", "format_name": "mp4"},
                "streams": streams,
            },
        )

    def test_preflight_failure_stops_before_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.mp4"
            source.write_bytes(b"source")
            config = AppConfig(
                work_dir=root / "work",
                ffprobe_bin="missing-ffprobe",
                seedance_model_id="ep-test",
            )
            ark = FakeNoopArk()
            pipeline = VideoLocalizationPipeline(
                config,
                ark_client=ark,
                uguu_client=FakeUguu(),
                seedance_client=FakeSeedance(),
            )
            with patch("core.pipeline.ffprobe.probe") as probe:
                with self.assertRaises(PreflightError):
                    pipeline.run(_spec(source))
                probe.assert_not_called()
            self.assertEqual(ark.calls, 0)

    def test_pipeline_generates_one_direct_seedance_audio_video(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.mp4"
            source.write_bytes(b"source")
            config = AppConfig(work_dir=root / "work", seedance_model_id="ep-test")
            events: list[dict] = []
            ark = FakeArk()
            uguu = FakeUguu()
            seedance = FakeSeedance()
            pipeline = VideoLocalizationPipeline(
                config,
                ark_client=ark,
                uguu_client=uguu,
                seedance_client=seedance,
                event_callback=events.append,
            )

            def fake_download(url, output_path, **kwargs):
                output_path = Path(output_path)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"seedance-final-video")
                return output_path

            with patch(
                "core.pipeline.download", side_effect=fake_download
            ), patch(
                "core.pipeline.ffprobe.probe", side_effect=lambda path, **kwargs: self._probe(Path(path))
            ):
                output = pipeline.run(_spec(source), skip_preflight=True)

            self.assertTrue(output.is_file())
            self.assertEqual(output.name, "final_ar-SA.mp4")
            stage_events = [
                event["stage"]
                for event in events
                if event.get("event_type") == "stage"
            ]
            self.assertEqual(stage_events, ["analyzing", "generating_video"])
            self.assertEqual(events[-1]["event_type"], "completed")
            self.assertEqual(len(ark.calls), 1)
            self.assertEqual(ark.calls[0][1]["response_format"], {"type": "json_object"})
            self.assertEqual([asset.kind for asset in uguu.assets], ["source_video"])
            self.assertEqual(len(seedance.create_calls), 1)
            content = seedance.create_calls[0][0]
            self.assertEqual([item["type"] for item in content], ["text", "video_url"])
            self.assertNotIn("audio_url", str(content))
            self.assertEqual(seedance.wait_calls, ["seedance-task"])

            job_id = next(event["job_id"] for event in events if event.get("job_id"))
            checkpoint = json.loads(
                (config.work_dir / job_id / "checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertEqual(checkpoint["pipeline_version"], PIPELINE_VERSION)
            self.assertEqual(checkpoint["stage"], "completed")
            self.assertEqual(checkpoint["metrics"]["speaker_count"], 1)
            self.assertEqual(checkpoint["metrics"]["dialogue_count"], 1)
            self.assertIn("localization_package", checkpoint["artifacts"])
            self.assertNotIn("localized_audio", checkpoint["artifacts"])
            self.assertNotIn("original_audio", checkpoint["artifacts"])
            self.assertNotIn("seed_audio", checkpoint["task_ids"])
            self.assertIn("seedance_duration", checkpoint["metrics"])
            self.assertNotIn("seed_audio_duration", checkpoint["metrics"])
            self.assertNotIn("mux_duration", checkpoint["metrics"])
            self.assertEqual(checkpoint["cache_key"]["target_locale"], "ar-SA")
            log_lines = [
                json.loads(line)
                for line in (config.work_dir / job_id / "job.log")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertTrue(any(line.get("target_locale") == "ar-SA" for line in log_lines))
            self.assertTrue(any("stage_duration_seconds" in line for line in log_lines))

    def test_pipeline_rejects_seedance_result_without_audio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.mp4"
            source.write_bytes(b"source")
            config = AppConfig(work_dir=root / "work", seedance_model_id="ep-test")
            events: list[dict] = []
            pipeline = VideoLocalizationPipeline(
                config,
                ark_client=FakeArk(),
                uguu_client=FakeUguu(),
                seedance_client=FakeSeedance(),
                event_callback=events.append,
            )

            def fake_download(url, output_path, **kwargs):
                Path(output_path).write_bytes(b"silent-video")
                return Path(output_path)

            probes = 0

            def fake_probe(path, **kwargs):
                nonlocal probes
                probes += 1
                return self._probe(Path(path), has_audio=probes == 1)

            with patch("core.pipeline.download", side_effect=fake_download), patch(
                "core.pipeline.ffprobe.probe", side_effect=fake_probe
            ):
                with self.assertRaisesRegex(ValidationError, "generated audio"):
                    pipeline.run(_spec(source), skip_preflight=True)
            self.assertTrue(any(event["event_type"] == "error" for event in events))

    def test_old_checkpoint_is_rejected_instead_of_falling_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.mp4"
            source.write_bytes(b"source")
            config = AppConfig(work_dir=root / "work")
            legacy_dir = config.work_dir / "legacy-job"
            legacy_dir.mkdir(parents=True)
            (legacy_dir / "checkpoint.json").write_text(
                json.dumps({"pipeline_version": 2, "stage": "generating_audio"}),
                encoding="utf-8",
            )
            pipeline = VideoLocalizationPipeline(config)
            with self.assertRaisesRegex(ValidationError, "start a new job"):
                pipeline._prepare_context(_spec(source), job_id="legacy-job")
