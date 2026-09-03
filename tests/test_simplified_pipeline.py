from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from api.common import ApiResponse
from api.minimax import MiniMaxTask
from config import AppConfig
from core.models import JobSpec, PipelineStage, UploadedAsset
from core.pipeline import VideoLocalizationPipeline
from media.ffprobe import MediaInfo
from utils.errors import ProviderError, ValidationError


class FakeMiniMax:
    logger = None

    def __init__(self, *, fail_context_ir: bool = False):
        self.fail_context_ir = fail_context_ir
        self.context_calls: list[dict[str, object]] = []
        self.video_calls: list[dict[str, object]] = []
        self.wait_calls: list[tuple[str, str]] = []

    def create_context_ir_task(self, content, *, duration, ratio, raw_dir=None):
        self.context_calls.append(
            {"content": content, "duration": duration, "ratio": ratio}
        )
        return MiniMaxTask(
            task_id=f"ir-{len(self.context_calls)}",
            request_id=f"ir-request-{len(self.context_calls)}",
            raw={"task_id": f"ir-{len(self.context_calls)}"},
            task_kind="minimax_context_ir",
        )

    def create_video_task(
        self, content, *, duration, resolution, ratio, raw_dir=None
    ):
        self.video_calls.append(
            {
                "content": content,
                "duration": duration,
                "resolution": resolution,
                "ratio": ratio,
            }
        )
        return MiniMaxTask(
            task_id=f"h3-{len(self.video_calls)}",
            request_id=f"h3-request-{len(self.video_calls)}",
            raw={"task_id": f"h3-{len(self.video_calls)}"},
            task_kind="minimax_h3",
        )

    def wait_task(self, task_id, *, task_kind, raw_dir=None, cancel_event=None):
        self.wait_calls.append((task_id, task_kind))
        if self.fail_context_ir and task_kind == "minimax_context_ir":
            raise ProviderError(
                "IR failed", provider=task_kind, error_code="FAILED", retryable=False
            )
        if task_kind == "minimax_context_ir":
            data = {
                "task_id": task_id,
                "status": "succeeded",
                "content": {"prompt": f"enhanced prompt for {task_id}"},
            }
        else:
            data = {
                "task_id": task_id,
                "status": "succeeded",
                "content": {"url": f"https://cdn.example/{task_id}.mp4"},
            }
        return ApiResponse(data=data, request_id=f"query-{task_id}")


class FakeUguu:
    logger = None

    def __init__(self):
        self.uploads: list[Path] = []

    def upload(self, path, *, kind, raw_dir=None):
        self.uploads.append(Path(path))
        return UploadedAsset(
            local_path=Path(path),
            remote_url=f"https://uguu.example/{Path(path).name}",
            uploaded_at="2026-09-03T00:00:00+00:00",
            kind=kind,
        )


class SimplifiedPipelineTests(unittest.TestCase):
    @staticmethod
    def _info(path: Path, duration: float) -> MediaInfo:
        return MediaInfo(
            path=path,
            duration=duration,
            streams=[{"codec_type": "video"}],
            format_name="mp4",
            raw={"format": {"duration": str(duration)}, "streams": []},
        )

    def _run_with_media_fakes(self, callback, *, source_duration=5.2):
        root = tempfile.TemporaryDirectory()
        source = Path(root.name) / "source.mp4"
        source.write_bytes(b"source")
        config = AppConfig(
            minimax_api_key="test-key",
            work_dir=Path(root.name) / "work",
            ffprobe_bin="ffprobe",
            poll_interval=0,
        )
        minimax = FakeMiniMax()
        uguu = FakeUguu()

        def probe(path, *, ffprobe_bin, timeout):
            path = Path(path)
            if path.name == "source.mp4":
                return self._info(path, source_duration)
            return self._info(path, 5.0)

        def normalize(source_path, destination, **kwargs):
            destination = Path(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(Path(source_path).read_bytes())
            return destination

        def download(url, output_path, **kwargs):
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"generated video")
            return output_path

        try:
            with (
                patch("core.pipeline.ffprobe.probe", side_effect=probe),
                patch("core.pipeline.normalize_video", side_effect=normalize),
                patch("core.pipeline.download", side_effect=download),
            ):
                pipeline = VideoLocalizationPipeline(
                    config,
                    minimax_client=minimax,
                    uguu_client=uguu,
                )
                result = callback(pipeline, source)
        except Exception:
            root.cleanup()
            raise
        return result, minimax, uguu, root

    def test_short_video_runs_exactly_context_ir_then_h3_once(self) -> None:
        def run(pipeline, source):
            return pipeline.run(
                JobSpec(input_video=source, target_locale="ar-SA"),
                skip_preflight=True,
            )

        result, minimax, uguu, root = self._run_with_media_fakes(run)

        self.assertEqual(result.stage, PipelineStage.COMPLETED)
        self.assertEqual(len(minimax.context_calls), 1)
        self.assertEqual(len(minimax.video_calls), 1)
        self.assertEqual(
            [kind for _, kind in minimax.wait_calls],
            ["minimax_context_ir", "minimax_h3"],
        )
        self.assertEqual(
            minimax.video_calls[0]["content"][0]["text"],
            "enhanced prompt for ir-1",
        )
        self.assertEqual(minimax.context_calls[0]["ratio"], "adaptive")
        self.assertEqual(minimax.video_calls[0]["ratio"], "adaptive")
        self.assertEqual(len(uguu.uploads), 1)
        self.assertIsNotNone(result.output_path)
        root.cleanup()

    def test_long_video_waits_then_processes_uploaded_segments_in_order(self) -> None:
        def run_and_append(pipeline, source):
            waiting = pipeline.run(
                JobSpec(input_video=source, target_locale="ar-SA"),
                skip_preflight=True,
            )
            self.assertEqual(waiting.stage, PipelineStage.WAITING_FOR_SEGMENTS)
            first = Path(source).with_name("segment-01.mp4")
            second = Path(source).with_name("segment-02.mp4")
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            pipeline.append_segment(first)
            pipeline.append_segment(second)
            return pipeline.finalize()

        with patch("core.pipeline.concat_videos", side_effect=self._fake_concat):
            result, minimax, uguu, root = self._run_with_media_fakes(
                run_and_append, source_duration=20.0
            )

        self.assertEqual(result.stage, PipelineStage.COMPLETED)
        self.assertEqual(len(minimax.context_calls), 2)
        self.assertEqual(len(minimax.video_calls), 2)
        self.assertEqual(
            [call["content"][0]["text"] for call in minimax.video_calls],
            ["enhanced prompt for ir-1", "enhanced prompt for ir-2"],
        )
        self.assertEqual(len(uguu.uploads), 2)
        self.assertEqual([path.name for path in uguu.uploads], [
            "segment_001_normalized.mp4",
            "segment_002_normalized.mp4",
        ])
        root.cleanup()

    @staticmethod
    def _fake_concat(sources, destination, **kwargs):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"concatenated")
        return destination

    def test_failed_segment_is_terminal_and_cannot_be_retried(self) -> None:
        def fail(pipeline, source):
            pipeline.minimax_client.fail_context_ir = True
            with self.assertRaises(ProviderError):
                pipeline.run(
                    JobSpec(input_video=source, target_locale="ar-SA"),
                    skip_preflight=True,
                )
            self.assertEqual(pipeline.job.stage, PipelineStage.FAILED)
            with self.assertRaises(ValidationError):
                pipeline.run(
                    JobSpec(input_video=source, target_locale="ar-SA"),
                    skip_preflight=True,
                )
            return pipeline.job.stage

        stage, minimax, _, root = self._run_with_media_fakes(fail)
        self.assertEqual(stage, PipelineStage.FAILED)
        self.assertEqual(len(minimax.context_calls), 1)
        self.assertEqual(len(minimax.video_calls), 0)
        root.cleanup()

    def test_source_segment_duration_is_checked_before_provider_calls(self) -> None:
        def run(pipeline, source):
            return pipeline.run(
                JobSpec(input_video=source, target_locale="ar-SA"),
                skip_preflight=True,
            )

        with self.assertRaises(ValidationError):
            self._run_with_media_fakes(run, source_duration=2.0)

    def test_three_second_segment_is_accepted_and_padded_to_h3_minimum(self) -> None:
        def run(pipeline, source):
            return pipeline.run(
                JobSpec(input_video=source, target_locale="ar-SA"),
                skip_preflight=True,
            )

        result, minimax, _, root = self._run_with_media_fakes(
            run, source_duration=3.0
        )
        self.assertEqual(result.stage, PipelineStage.COMPLETED)
        self.assertEqual(minimax.context_calls[0]["duration"], 4)
        self.assertEqual(minimax.video_calls[0]["duration"], 4)
        self.assertEqual(
            self._duration_from_pipeline(result, root),
            4,
        )
        root.cleanup()

    @staticmethod
    def _duration_from_pipeline(result, root):
        del result
        manifests = list((Path(root.name) / "work").glob("*/json/session.json"))
        if not manifests:
            return None
        import json

        payload = json.loads(manifests[0].read_text(encoding="utf-8"))
        return payload["segments"][0]["normalized_duration_seconds"]


if __name__ == "__main__":
    unittest.main()
