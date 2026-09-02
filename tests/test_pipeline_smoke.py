from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from api.common import ApiResponse
from api.seedance import SeedanceTask
from config import AppConfig
from core.models import JobSpec, UploadedAsset
from core.pipeline import PIPELINE_VERSION, VideoLocalizationPipeline
from media.ffprobe import MediaInfo
from utils.errors import PreflightError, ProviderError, ValidationError


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


class FailThenSucceedSeedance(FakeSeedance):
    def create_task(self, content, **kwargs):
        self.create_calls.append((content, kwargs))
        task_id = f"seedance-task-{len(self.create_calls)}"
        return SeedanceTask(task_id, f"seedance-req-{len(self.create_calls)}", {"id": task_id})

    def wait_task(self, task_id, **kwargs):
        self.wait_calls.append(task_id)
        if len(self.wait_calls) == 1:
            raise ProviderError(
                "Seedance task ended with status failed",
                provider="seedance",
                error_code="FAILED",
                request_id="seedance-failed-query",
                payload={"id": task_id, "status": "failed"},
                retryable=False,
            )
        return ApiResponse(
            {
                "id": task_id,
                "status": "succeeded",
                "content": {"video_url": "https://example.test/video.mp4"},
            },
            "seedance-success-query",
        )


class TimeoutThenSucceedSeedance(FakeSeedance):
    def create_task(self, content, **kwargs):
        self.create_calls.append((content, kwargs))
        return SeedanceTask("seedance-active-task", "seedance-create", {"id": "seedance-active-task"})

    def wait_task(self, task_id, **kwargs):
        self.wait_calls.append(task_id)
        if len(self.wait_calls) == 1:
            raise ProviderError(
                "Seedance task polling timed out",
                provider="seedance",
                error_code="TASK_TIMEOUT",
                request_id="seedance-timeout-query",
                retryable=False,
            )
        return ApiResponse(
            {
                "id": task_id,
                "status": "succeeded",
                "content": {"video_url": "https://example.test/video.mp4"},
            },
            "seedance-success-query",
        )


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
                output = pipeline.run(
                    _spec(source),
                    skip_preflight=True,
                    execution_mode="auto",
                )

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
                    pipeline.run(
                        _spec(source),
                        skip_preflight=True,
                        execution_mode="auto",
                    )
            self.assertTrue(any(event["event_type"] == "error" for event in events))

    def test_manual_mode_pauses_after_doubao_until_approved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.mp4"
            source.write_bytes(b"source")
            config = AppConfig(work_dir=root / "work", seedance_model_id="ep-test")
            ark = FakeArk()
            seedance = FakeSeedance()
            pipeline = VideoLocalizationPipeline(
                config,
                ark_client=ark,
                uguu_client=FakeUguu(),
                seedance_client=seedance,
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
                paused = pipeline.run(_spec(source), skip_preflight=True)
                self.assertEqual(paused.stage, "waiting_for_approval")
                self.assertEqual(paused.action_required, "approve_seedance")
                self.assertEqual(len(ark.calls), 1)
                self.assertEqual(len(seedance.create_calls), 0)
                self.assertIsNotNone(paused.package_path)

                job_id = paused.job_id
                checkpoint_path = config.work_dir / job_id / "checkpoint.json"
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                self.assertEqual(checkpoint["stage"], "waiting_for_approval")
                self.assertTrue((config.work_dir / job_id / "json/localization_package.json").is_file())

                completed = pipeline.approve_seedance(job_id)

            self.assertTrue(completed.is_file())
            self.assertEqual(len(ark.calls), 1)
            self.assertEqual(len(seedance.create_calls), 1)
            final_checkpoint = json.loads(
                (config.work_dir / job_id / "checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertEqual(final_checkpoint["stage"], "completed")
            self.assertEqual(
                [item["node"] for item in final_checkpoint["node_executions"]],
                ["doubao", "seedance"],
            )
            self.assertTrue((config.work_dir / "history.json").is_file())

    def test_recovery_after_saved_doubao_result_does_not_call_doubao_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.mp4"
            source.write_bytes(b"source")
            config = AppConfig(work_dir=root / "work", seedance_model_id="ep-test")
            ark = FakeArk()
            seedance = FakeSeedance()
            pipeline = VideoLocalizationPipeline(
                config,
                ark_client=ark,
                uguu_client=FakeUguu(),
                seedance_client=seedance,
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
                paused = pipeline.run(_spec(source), skip_preflight=True)
                checkpoint_path = config.work_dir / paused.job_id / "checkpoint.json"
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                checkpoint["stage"] = "analyzing"
                checkpoint["approval_status"] = "not_required"
                checkpoint_path.write_text(
                    json.dumps(checkpoint, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

                restarted = VideoLocalizationPipeline(
                    config,
                    ark_client=ark,
                    uguu_client=FakeUguu(),
                    seedance_client=seedance,
                )
                recovered = restarted.resume_failed(paused.job_id)
                self.assertEqual(recovered.stage, "waiting_for_approval")
                completed = restarted.approve_seedance(paused.job_id)

            self.assertTrue(completed.is_file())
            self.assertEqual(len(ark.calls), 1)
            self.assertEqual(len(seedance.create_calls), 1)

    def test_seedance_retry_reuses_doubao_and_keeps_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.mp4"
            source.write_bytes(b"source")
            config = AppConfig(work_dir=root / "work", seedance_model_id="ep-test")
            ark = FakeArk()
            seedance = FailThenSucceedSeedance()
            pipeline = VideoLocalizationPipeline(
                config,
                ark_client=ark,
                uguu_client=FakeUguu(),
                seedance_client=seedance,
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
                paused = pipeline.run(_spec(source), skip_preflight=True)
                with self.assertRaises(ProviderError):
                    pipeline.approve_seedance(paused.job_id)
                failed_checkpoint = json.loads(
                    (config.work_dir / paused.job_id / "checkpoint.json").read_text(encoding="utf-8")
                )
                self.assertEqual(failed_checkpoint["stage"], "failed")
                self.assertEqual(len(ark.calls), 1)
                self.assertEqual(len(seedance.create_calls), 1)

                result = pipeline.retry_seedance(paused.job_id)

            self.assertTrue(result.is_file())
            self.assertEqual(len(ark.calls), 1)
            self.assertEqual(len(seedance.create_calls), 2)
            self.assertEqual(seedance.wait_calls, ["seedance-task-1", "seedance-task-2"])
            checkpoint = json.loads(
                (config.work_dir / paused.job_id / "checkpoint.json").read_text(encoding="utf-8")
            )
            seedance_nodes = [
                item for item in checkpoint["node_executions"] if item["node"] == "seedance"
            ]
            self.assertEqual([item["attempt"] for item in seedance_nodes], [1, 2])
            self.assertEqual([item["status"] for item in seedance_nodes], ["failed", "completed"])
            self.assertTrue(
                (config.work_dir / paused.job_id / "json/nodes/seedance/attempt_001/failure.json").is_file()
            )
            self.assertTrue(
                (config.work_dir / paused.job_id / "json/nodes/seedance/attempt_002/result.json").is_file()
            )

    def test_continue_seedance_reuses_active_task_after_poll_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.mp4"
            source.write_bytes(b"source")
            config = AppConfig(work_dir=root / "work", seedance_model_id="ep-test")
            ark = FakeArk()
            seedance = TimeoutThenSucceedSeedance()
            pipeline = VideoLocalizationPipeline(
                config,
                ark_client=ark,
                uguu_client=FakeUguu(),
                seedance_client=seedance,
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
                paused = pipeline.run(_spec(source), skip_preflight=True)
                with self.assertRaises(ProviderError):
                    pipeline.approve_seedance(paused.job_id)

                restarted = VideoLocalizationPipeline(
                    config,
                    ark_client=ark,
                    uguu_client=FakeUguu(),
                    seedance_client=seedance,
                )
                result = restarted.continue_seedance(paused.job_id)

            self.assertTrue(result.is_file())
            self.assertEqual(len(ark.calls), 1)
            self.assertEqual(len(seedance.create_calls), 1)
            self.assertEqual(seedance.wait_calls, ["seedance-active-task", "seedance-active-task"])

    def test_seedance_retry_refreshes_expired_uguu_without_reanalyzing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.mp4"
            source.write_bytes(b"source")
            config = AppConfig(work_dir=root / "work", seedance_model_id="ep-test")
            ark = FakeArk()
            uguu = FakeUguu()
            seedance = FailThenSucceedSeedance()
            pipeline = VideoLocalizationPipeline(
                config,
                ark_client=ark,
                uguu_client=uguu,
                seedance_client=seedance,
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
                paused = pipeline.run(_spec(source), skip_preflight=True)
                with self.assertRaises(ProviderError):
                    pipeline.approve_seedance(paused.job_id)

                assets_path = config.work_dir / paused.job_id / "json/assets.json"
                assets = json.loads(assets_path.read_text(encoding="utf-8"))
                expired_at = (
                    datetime.now(timezone.utc) - timedelta(hours=4)
                ).isoformat()
                for asset in assets:
                    asset["uploaded_at"] = expired_at
                assets_path.write_text(
                    json.dumps(assets, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

                result = pipeline.retry_seedance(paused.job_id)

            self.assertTrue(result.is_file())
            self.assertEqual(len(ark.calls), 1)
            self.assertEqual(len(uguu.assets), 2)

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
