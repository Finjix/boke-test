from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from api.common import ApiResponse
from config import AppConfig
from core.models import JobSpec, UploadedAsset
from core.h3_prompt import build_transformation_prompt
from core.pipeline import VideoLocalizationPipeline
from media.ffprobe import MediaInfo
from utils.errors import ProviderError, ValidationError


def _info(path: Path, duration: float) -> MediaInfo:
    streams = [{"codec_type": "video"}, {"codec_type": "audio"}]
    return MediaInfo(
        path=Path(path),
        duration=duration,
        streams=streams,
        format_name="mp4",
        raw={"format": {"duration": str(duration)}, "streams": streams},
    )


class FakeUguu:
    def __init__(self) -> None:
        self.assets: list[UploadedAsset] = []

    def upload(self, path, *, kind="reference", **kwargs):
        asset = UploadedAsset(
            local_path=Path(path).resolve(),
            remote_url=f"https://example.test/{len(self.assets)}-{Path(path).name}",
            uploaded_at=datetime.now(timezone.utc).isoformat(),
            kind=kind,
        )
        self.assets.append(asset)
        return asset

    def check_access(self, **kwargs):
        return None


class FakeH3:
    def __init__(self, *, fail_first=False, timeout_first=False) -> None:
        self.create_calls: list[tuple[list[dict], dict]] = []
        self.wait_calls: list[str] = []
        self.fail_first = fail_first
        self.timeout_first = timeout_first

    def create_task(self, content, **kwargs):
        self.create_calls.append((content, kwargs))
        task_id = f"h3-task-{len(self.create_calls)}"
        return SimpleNamespace(
            task_id=task_id,
            request_id=f"h3-request-{len(self.create_calls)}",
            raw={"task_id": task_id},
            raw_path=None,
        )

    def wait_task(self, task_id, **kwargs):
        self.wait_calls.append(task_id)
        if self.fail_first and len(self.wait_calls) == 1:
            raise ProviderError(
                "MiniMax task failed",
                provider="minimax_h3",
                error_code="FAILED",
                request_id="failure-request",
                payload={"task_id": task_id, "status": "failed"},
                retryable=False,
            )
        if self.timeout_first and len(self.wait_calls) == 1:
            raise ProviderError(
                "MiniMax task polling timed out",
                provider="minimax_h3",
                error_code="TASK_TIMEOUT",
                request_id="timeout-request",
                retryable=False,
            )
        return ApiResponse(
            {
                "task": {
                    "task_id": task_id,
                    "status": "succeeded",
                    "content": {"url": "https://example.test/generated.mp4"},
                }
            },
            "success-request",
        )

    def check_access(self, **kwargs):
        return None


class H3WorkflowTests(unittest.TestCase):
    def _config(self, root: Path) -> AppConfig:
        return AppConfig(
            work_dir=root / "work",
            ffprobe_bin="ffprobe",
            minimax_api_key="test-key",
            poll_interval=0,
        )

    def _spec(self, source: Path, refs=None) -> JobSpec:
        return JobSpec(
            input_video=source,
            target_language="ar",
            target_region="Saudi Arabia",
            target_locale="ar-SA",
            reference_images=list(refs or []),
            transformation_instruction="Keep the creative structure and localize the setting.",
        )

    @staticmethod
    def _normalize(source, destination, **kwargs):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(Path(source).read_bytes())
        return destination

    @staticmethod
    def _download(url, output_path, **kwargs):
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"generated")
        return output_path

    def test_short_video_direct_h3_without_doubao(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            h3 = FakeH3()
            with patch("core.h3_pipeline.ffprobe.probe", return_value=_info(source, 5.75)), patch(
                "core.h3_pipeline.normalize_video", side_effect=self._normalize
            ), patch("core.h3_pipeline.download", side_effect=self._download):
                result = VideoLocalizationPipeline(
                    self._config(root),
                    minimax_client=h3,
                    uguu_client=FakeUguu(),
                ).run(self._spec(source), skip_preflight=True)

            self.assertEqual(result.stage.value, "completed")
            self.assertEqual(len(h3.create_calls), 1)
            self.assertEqual([item["type"] for item in h3.create_calls[0][0]], ["text", "video_url"])
            checkpoint = json.loads((root / "work" / result.job_id / "checkpoint.json").read_text())
            self.assertEqual(checkpoint["pipeline_version"], 5)
            self.assertEqual(checkpoint["provider"], "minimax_h3")
            self.assertEqual(checkpoint["h3_segments"][0]["normalized_duration_seconds"], 6)

    def test_default_prompt_requires_full_scene_transformation(self) -> None:
        prompt = build_transformation_prompt(
            target_language="ar",
            target_region="Saudi Arabia",
            target_locale="ar-SA",
        )
        self.assertIn("not a dubbing-only", prompt)
        self.assertIn("Mandatory full-scene transformation", prompt)
        self.assertIn("Do not leave the original location", prompt)
        self.assertIn("Keep the transformed people, environment", prompt)

    def test_successful_output_with_container_drift_is_normalized_locally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            h3 = FakeH3()

            def probe(path, **kwargs):
                name = Path(path).name
                if name == "provider_output.mp4":
                    duration = 6.584
                elif name == "output.mp4":
                    duration = 6.0
                else:
                    duration = 5.75
                return _info(Path(path), duration)

            with patch("core.h3_pipeline.ffprobe.probe", side_effect=probe), patch(
                "core.h3_pipeline.normalize_video", side_effect=self._normalize
            ), patch("core.h3_pipeline.download", side_effect=self._download):
                result = VideoLocalizationPipeline(
                    self._config(root), minimax_client=h3, uguu_client=FakeUguu()
                ).run(self._spec(source), skip_preflight=True)

            self.assertEqual(result.stage.value, "completed")
            job_dir = root / "work" / result.job_id
            attempt = json.loads(
                (job_dir / "checkpoint.json").read_text(encoding="utf-8")
            )["h3_segments"][0]["attempts"][0]
            self.assertTrue(attempt["provider_output_artifact"].endswith("provider_output.mp4"))
            self.assertTrue((job_dir / "json/nodes/h3/segment_001/attempt_001/provider_output.mp4").is_file())

    def test_long_video_waits_without_creating_task_and_uses_original_frames_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = root / "master.mp4"
            first = root / "first.mp4"
            second = root / "second.mp4"
            master.write_bytes(b"master")
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            h3 = FakeH3()
            uguu = FakeUguu()
            fake_frames = [root / f"frame-{index}.png" for index in range(4)]
            for frame in fake_frames:
                frame.write_bytes(b"frame")

            def probe(path, **kwargs):
                path = Path(path)
                if path.name == "source_master.mp4":
                    duration = 16.5
                elif path.name.startswith("final_"):
                    duration = 16.0
                else:
                    duration = 8.0
                return _info(path, duration)

            def fake_concat(sources, destination, **kwargs):
                destination = Path(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"concatenated")
                return destination

            with patch("core.h3_pipeline.ffprobe.probe", side_effect=probe), patch(
                "core.h3_pipeline.normalize_video", side_effect=self._normalize
            ), patch("core.h3_pipeline.extract_uniform_frames", return_value=fake_frames), patch(
                "core.h3_pipeline.download", side_effect=self._download
            ), patch("core.h3_pipeline.concat_videos", side_effect=fake_concat):
                pipeline = VideoLocalizationPipeline(
                    self._config(root), minimax_client=h3, uguu_client=uguu
                )
                waiting = pipeline.run(self._spec(master), skip_preflight=True)
                self.assertEqual(waiting.stage.value, "waiting_for_segments")
                self.assertEqual(len(h3.create_calls), 0)
                self.assertEqual(waiting.next_segment_index, 1)
                first_result = pipeline.append_segment(waiting.job_id, first)
                self.assertEqual(first_result.stage.value, "waiting_for_next_segment")
                second_result = pipeline.append_segment(waiting.job_id, second)
                self.assertEqual(second_result.stage.value, "waiting_for_next_segment")
                final = pipeline.finalize(waiting.job_id)

            self.assertEqual(final.stage.value, "completed")
            self.assertEqual(len(h3.create_calls), 2)
            second_content = h3.create_calls[1][0]
            self.assertEqual([item["type"] for item in second_content], [
                "text", "video_url", "image_url", "image_url", "image_url", "image_url"
            ])
            self.assertEqual(
                [item.get("role") for item in second_content[1:]],
                ["reference_video", "reference_image", "reference_image", "reference_image", "reference_image"],
            )

    def test_previous_generated_video_is_used_when_reference_budget_allows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = root / "master.mp4"
            first = root / "first.mp4"
            second = root / "second.mp4"
            for path in (master, first, second):
                path.write_bytes(path.name.encode())
            h3 = FakeH3()

            def probe(path, **kwargs):
                return _info(Path(path), 16.5 if Path(path).name == "source_master.mp4" else 4.0)

            with patch("core.h3_pipeline.ffprobe.probe", side_effect=probe), patch(
                "core.h3_pipeline.normalize_video", side_effect=self._normalize
            ), patch("core.h3_pipeline.download", side_effect=self._download):
                pipeline = VideoLocalizationPipeline(
                    self._config(root), minimax_client=h3, uguu_client=FakeUguu()
                )
                waiting = pipeline.run(self._spec(master), skip_preflight=True)
                pipeline.append_segment(waiting.job_id, first)
                pipeline.append_segment(waiting.job_id, second)

            second_content = h3.create_calls[1][0]
            self.assertEqual([item["type"] for item in second_content], ["text", "video_url", "video_url"])
            self.assertEqual(
                [item.get("role") for item in second_content[1:]],
                ["reference_video", "reference_video"],
            )

    def test_failed_segment_retry_creates_new_task_without_repeating_any_other_step(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            h3 = FakeH3(fail_first=True)
            with patch("core.h3_pipeline.ffprobe.probe", return_value=_info(source, 5.0)), patch(
                "core.h3_pipeline.normalize_video", side_effect=self._normalize
            ), patch("core.h3_pipeline.download", side_effect=self._download):
                pipeline = VideoLocalizationPipeline(
                    self._config(root), minimax_client=h3, uguu_client=FakeUguu()
                )
                with self.assertRaises(ProviderError):
                    pipeline.run(self._spec(source), skip_preflight=True)
                # Obtain the only job ID from the persisted history without
                # relying on a provider response or a GUI selection.
                entries = pipeline.history_store.list_entries()
                job_id = entries[0].job_id
                result = pipeline.retry_segment(job_id)

            self.assertEqual(result.stage.value, "completed")
            self.assertEqual(len(h3.create_calls), 2)
            checkpoint = json.loads((root / "work" / job_id / "checkpoint.json").read_text())
            attempts = checkpoint["h3_segments"][0]["attempts"]
            self.assertEqual([item["status"] for item in attempts], ["failed", "completed"])
            self.assertTrue((root / "work" / job_id / "json/nodes/h3/segment_001/attempt_001/failure.json").is_file())

    def test_timeout_keeps_task_id_and_continue_after_restart_does_not_create_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            h3 = FakeH3(timeout_first=True)
            with patch("core.h3_pipeline.ffprobe.probe", return_value=_info(source, 5.0)), patch(
                "core.h3_pipeline.normalize_video", side_effect=self._normalize
            ), patch("core.h3_pipeline.download", side_effect=self._download):
                pipeline = VideoLocalizationPipeline(
                    self._config(root), minimax_client=h3, uguu_client=FakeUguu()
                )
                with self.assertRaises(ProviderError):
                    pipeline.run(self._spec(source), skip_preflight=True)
                job_id = pipeline.history_store.list_entries()[0].job_id
                restarted = VideoLocalizationPipeline(
                    self._config(root), minimax_client=h3, uguu_client=FakeUguu()
                )
                result = restarted.continue_segment(job_id)

            self.assertEqual(result.stage.value, "completed")
            self.assertEqual(len(h3.create_calls), 1)
            self.assertEqual(h3.wait_calls, ["h3-task-1", "h3-task-1"])

    def test_reference_images_are_limited_to_nine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            refs = []
            for index in range(10):
                path = root / f"ref-{index}.png"
                path.write_bytes(b"ref")
                refs.append(path)
            with self.assertRaises(ValueError):
                self._spec(source, refs)


if __name__ == "__main__":
    unittest.main()
