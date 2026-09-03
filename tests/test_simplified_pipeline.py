from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from api.common import ApiResponse
from api.minimax import MiniMaxTask
from config import AppConfig
from core.models import JobSpec, PipelineEvent, PipelineStage, generation_duration
from core.pipeline import VideoLocalizationPipeline
from media.ffprobe import MediaInfo
from utils.errors import ProviderError, ValidationError


class FakeMiniMax:
    def __init__(self, *, fail_context: bool = False):
        self.fail_context = fail_context
        self.context_calls: list[dict[str, object]] = []
        self.video_calls: list[dict[str, object]] = []
        self.wait_calls: list[tuple[str, str]] = []

    def create_context_ir_task(self, content, *, duration, ratio):
        self.context_calls.append(
            {"content": content, "duration": duration, "ratio": ratio}
        )
        number = len(self.context_calls)
        return MiniMaxTask(f"ir-{number}", f"ir-request-{number}")

    def create_video_task(self, content, *, duration, resolution, ratio):
        self.video_calls.append(
            {
                "content": content,
                "duration": duration,
                "resolution": resolution,
                "ratio": ratio,
            }
        )
        number = len(self.video_calls)
        return MiniMaxTask(f"h3-{number}", f"h3-request-{number}")

    def wait_task(self, task_id, *, task_kind, cancel_event=None):
        self.wait_calls.append((task_id, task_kind))
        if self.fail_context and task_kind == "H3-Context-IR":
            raise ProviderError(
                "Context-IR failed",
                provider=task_kind,
                error_code="FAILED",
            )
        if task_kind == "H3-Context-IR":
            return ApiResponse(
                data={
                    "task_id": task_id,
                    "status": "succeeded",
                    "content": {"prompt": f"enhanced-{task_id}"},
                },
                request_id=f"query-{task_id}",
            )
        return ApiResponse(
            data={
                "task_id": task_id,
                "status": "succeeded",
                "content": {"url": f"https://cdn.example/{task_id}.mp4"},
            },
            request_id=f"query-{task_id}",
        )


class SimplifiedPipelineTests(unittest.TestCase):
    @staticmethod
    def _info(
        path: Path,
        duration: float,
        *,
        has_video: bool = True,
        has_audio: bool = True,
    ) -> MediaInfo:
        streams = []
        if has_video:
            streams.append({"codec_type": "video"})
        if has_audio:
            streams.append({"codec_type": "audio"})
        return MediaInfo(
            path=path,
            duration=duration,
            streams=streams,
            format_name="mp4",
            raw={"format": {"duration": str(duration)}, "streams": []},
        )

    def _run(
        self,
        *,
        source_duration: float = 5.2,
        references: int = 0,
        fail_context: bool = False,
        source_exists: bool = True,
        source_has_video: bool = True,
        source_has_audio: bool = True,
    ):
        root = tempfile.TemporaryDirectory()
        root_path = Path(root.name)
        source = root_path / "source.mkv"
        if source_exists:
            source.write_bytes(b"source")
        reference_paths = []
        for index in range(references):
            reference_path = root_path / f"reference-{index + 1}.png"
            reference_path.write_bytes(f"reference-{index + 1}".encode())
            reference_paths.append(reference_path)
        config = AppConfig(
            minimax_api_key="test-key",
            work_dir=root_path / "work",
            output_dir=root_path / "output",
            ffprobe_bin="ffprobe",
            ffmpeg_bin="ffmpeg",
            poll_interval=0,
        )
        minimax = FakeMiniMax(fail_context=fail_context)
        events: list[PipelineEvent] = []

        def probe(path, *, ffprobe_bin, timeout):
            path = Path(path)
            if path == source:
                return self._info(
                    path,
                    source_duration,
                    has_video=source_has_video,
                    has_audio=source_has_audio,
                )
            return self._info(path, 5.0)

        def normalize(source_path, destination, **kwargs):
            destination = Path(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(Path(source_path).read_bytes())

        def fake_download(url, output_path, **kwargs):
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"generated")
            return output_path

        pipeline = VideoLocalizationPipeline(
            config,
            minimax_client=minimax,
            event_callback=events.append,
        )
        spec = JobSpec(
            input_video=source,
            reference_images=tuple(reference_paths),
            target_locale="ar-SA",
        )
        patches = (
            patch("core.pipeline.ffprobe.probe", side_effect=probe),
            patch("core.pipeline.normalize_video", side_effect=normalize),
            patch("core.pipeline.download", side_effect=fake_download),
        )
        for item in patches:
            item.start()
        try:
            result = pipeline.run(spec, skip_preflight=True)
        except Exception:
            root.cleanup()
            raise
        finally:
            for item in reversed(patches):
                item.stop()
        return result, minimax, events, root

    def test_one_video_runs_context_ir_then_h3_once(self) -> None:
        result, minimax, events, root = self._run()
        try:
            self.assertEqual(result.stage, PipelineStage.COMPLETED)
            self.assertEqual(result.duration_seconds, 5)
            self.assertEqual(len(minimax.context_calls), 1)
            self.assertEqual(len(minimax.video_calls), 1)
            self.assertEqual(
                minimax.wait_calls,
                [("ir-1", "H3-Context-IR"), ("h3-1", "MiniMax-H3")],
            )
            self.assertEqual(len(minimax.context_calls[0]["content"]), 2)
            generation_prompt = minimax.video_calls[0]["content"][0]["text"]
            self.assertIn("enhanced-ir-1", generation_prompt)
            self.assertIn("本地化硬性验收规则", generation_prompt)
            self.assertIn("不得保留或生成源语言文字、拉丁字母招牌", generation_prompt)
            self.assertRegex(result.output_path.name, r"^\d{8}_\d{6}(?:_\d{2})?\.mp4$")
            self.assertTrue(result.output_path.is_file())
            self.assertEqual(list((Path(root.name) / "work").iterdir()), [])
            self.assertEqual(
                events[-1].stage,
                PipelineStage.COMPLETED,
            )
        finally:
            root.cleanup()

    def test_three_second_video_is_submitted_as_four_seconds(self) -> None:
        result, minimax, _, root = self._run(source_duration=3.0)
        try:
            self.assertEqual(result.duration_seconds, 4)
            self.assertEqual(minimax.context_calls[0]["duration"], 4)
            self.assertEqual(minimax.video_calls[0]["duration"], 4)
        finally:
            root.cleanup()

    def test_generation_duration_uses_half_up_rounding(self) -> None:
        self.assertEqual(generation_duration(3.0), 4)
        self.assertEqual(generation_duration(4.49), 4)
        self.assertEqual(generation_duration(4.5), 5)
        self.assertEqual(generation_duration(5.2), 5)
        self.assertEqual(generation_duration(14.5), 15)

    def test_multiple_reference_images_are_reused_in_both_calls(self) -> None:
        _, minimax, _, root = self._run(references=3)
        try:
            context_content = minimax.context_calls[0]["content"]
            video_content = minimax.video_calls[0]["content"]
            self.assertEqual(
                [item["type"] for item in context_content],
                ["text", "image_url", "image_url", "image_url", "video_url"],
            )
            self.assertEqual(
                [item["role"] for item in context_content[1:]],
                [
                    "reference_image",
                    "reference_image",
                    "reference_image",
                    "reference_video",
                ],
            )
            self.assertEqual(context_content[1:], video_content[1:])
        finally:
            root.cleanup()

    def test_invalid_duration_stops_before_provider_calls(self) -> None:
        with self.assertRaises(ValidationError):
            self._run(source_duration=2.99)

    def test_duration_above_limit_stops_before_provider_calls(self) -> None:
        with self.assertRaises(ValidationError):
            self._run(source_duration=15.01)

    def test_missing_source_stops_before_provider_calls(self) -> None:
        with self.assertRaises(ValidationError):
            self._run(source_exists=False)

    def test_source_without_video_stream_stops_before_provider_calls(self) -> None:
        with self.assertRaises(ValidationError):
            self._run(source_has_video=False)

    def test_context_ir_failure_does_not_start_h3(self) -> None:
        with self.assertRaises(ProviderError):
            self._run(fail_context=True)

    def test_request_size_limit_stops_before_provider_calls(self) -> None:
        with patch("core.h3_prompt.MINIMAX_MAX_REQUEST_BYTES", 10):
            with self.assertRaises(ValidationError):
                self._run()


if __name__ == "__main__":
    unittest.main()
