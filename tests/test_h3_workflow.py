from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from api.common import ApiResponse
from api.ark import extract_text
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


class FakeArk:
    def __init__(
        self,
        payload: dict,
        *,
        fail_first: bool = False,
        image_fail_first: bool = False,
    ) -> None:
        self.payload = payload
        self.fail_first = fail_first
        self.image_fail_first = image_fail_first
        self.chat_calls: list[tuple[list[dict], dict]] = []
        self.image_calls: list[tuple[list[str], str, dict]] = []
        self.last_request_id: str | None = None

    @staticmethod
    def extract_text(response):
        return extract_text(response)

    def chat(self, messages, **kwargs):
        self.chat_calls.append((messages, kwargs))
        if self.fail_first and len(self.chat_calls) == 1:
            raise ProviderError(
                "Doubao analysis failed",
                provider="ark",
                error_code="ANALYSIS_FAILED",
                request_id="doubao-request-1",
                retryable=False,
            )
        request_id = f"doubao-request-{len(self.chat_calls)}"
        return ApiResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(self.payload, ensure_ascii=False),
                        }
                    }
                ]
            },
            request_id,
        )

    def generate_image(self, image_urls, prompt, **kwargs):
        self.image_calls.append((list(image_urls), prompt, kwargs))
        request_id = f"seedream-request-{len(self.image_calls)}"
        self.last_request_id = request_id
        if self.image_fail_first and len(self.image_calls) == 1:
            raise ProviderError(
                "Seedream image generation failed",
                provider="seedream",
                error_code="IMAGE_FAILED",
                request_id=request_id,
                retryable=False,
            )
        return ApiResponse(
            {
                "data": [{"url": f"https://example.test/seedream-{len(self.image_calls)}.png"}],
            },
            request_id,
        )


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
            ark_api_key="test-ark-key",
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
    def _doubao_payload(duration_ms: int = 5000) -> dict:
        split = duration_ms // 2
        reference_shots = [
            {
                "shot_id": "shot_001",
                "start_ms": 0,
                "end_ms": split,
                "keyframe_ms": min(1000, max(1, split // 2)),
                "character_ids": ["speaker_1"],
                "continuity_group": "main-scene",
                "scene_description": "Target-region street storefront with the same camera geography.",
                "replacement_requirements": [
                    "replace the source facade, storefront identity and all visible signs",
                    "localize wardrobe, vehicle and packaging for Saudi Arabia",
                ],
                "preserve_requirements": [
                    "same character relationship, pose, object placement and composition",
                ],
                "seedream_prompt": "Rebuild the complete background and wardrobe in the target region.",
            }
        ]
        if duration_ms > 10000:
            reference_shots.append(
                {
                    "shot_id": "shot_002",
                    "start_ms": split,
                    "end_ms": duration_ms,
                    "keyframe_ms": split + max(1, (duration_ms - split) // 2),
                    "character_ids": ["speaker_1"],
                    "continuity_group": "main-scene",
                    "scene_description": "The same localized street scene from a later camera beat.",
                    "replacement_requirements": [
                        "keep the target-region architecture and redraw signs and packaging",
                    ],
                    "preserve_requirements": [
                        "same character relationship, action timing and camera rhythm",
                    ],
                    "seedream_prompt": "Maintain the localized scene continuity while replacing source details.",
                }
            )
        return {
            "source": {"language": "zh"},
            "target": {
                "language": "ar",
                "region": "Saudi Arabia",
                "locale": "ar-SA",
            },
            "video_analysis": {
                "shot_plan": "Two-shot office conversation; preserve the camera path and timing.",
            },
            "speakers": [{"id": "speaker_1", "visual_hint": "adult presenter"}],
            "dialogues": [
                {
                    "speaker_id": "speaker_1",
                    "start_ms": 0,
                    "end_ms": 1000,
                    "source_text": "Hello",
                    "target_text": "مرحبا",
                }
            ],
            "visual_localization": {
                "scene": "Riyadh office and Arabic storefront signage",
                "visible_text": "translate existing signs in place",
            },
            "cultural_requirements": ["Use authentic Saudi architecture and signage."],
            "reference_shots": reference_shots,
            "h3_prompt": (
                "Shot-by-shot localization: transform the presenter wardrobe and the Riyadh "
                "office and Arabic storefront signage into an authentic Saudi setting; replace "
                "visible signs and packaging "
                "with Arabic text in the same positions and timing; keep the same person, role, "
                "props, camera path, composition, actions, shot order, transitions, rhythm and "
                "creative effect. Generate stable Arabic speech for speaker_1 with matching "
                "emotion, timing and lip sync, while preserving sound-design intent."
            ),
        }

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
        if output_path.suffix.lower() == ".png":
            # Minimal valid 2x2 PNG header; the local validator only needs a
            # real image signature and dimensions for this provider stub.
            output_path.write_bytes(
                b"\x89PNG\r\n\x1a\n"
                b"\x00\x00\x00\rIHDR\x00\x00\x00\x02\x00\x00\x00\x02"
                b"\x08\x06\x00\x00\x00\x72\xb6\x0d\x24"
            )
        else:
            output_path.write_bytes(b"generated")
        return output_path

    @staticmethod
    def _extract_frame(source, destination, **kwargs):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\rIHDR\x00\x00\x00\x02\x00\x00\x00\x02"
            b"\x08\x06\x00\x00\x00\x72\xb6\x0d\x24"
        )
        return destination

    def test_short_video_runs_doubao_then_h3_in_auto_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            ark = FakeArk(self._doubao_payload())
            h3 = FakeH3()
            with patch("core.h3_pipeline.ffprobe.probe", return_value=_info(source, 5.75)), patch(
                "core.h3_pipeline.normalize_video", side_effect=self._normalize
            ), patch("core.h3_pipeline.extract_frame_at", side_effect=self._extract_frame), patch(
                "core.h3_pipeline.download", side_effect=self._download
            ):
                result = VideoLocalizationPipeline(
                    self._config(root),
                    ark_client=ark,
                    minimax_client=h3,
                    uguu_client=FakeUguu(),
                ).run(self._spec(source), execution_mode="auto", skip_preflight=True)

            self.assertEqual(result.stage.value, "completed")
            self.assertEqual(len(ark.chat_calls), 1)
            self.assertEqual(len(ark.image_calls), 1)
            analysis_user_text = ark.chat_calls[0][0][1]["content"][1]["text"]
            self.assertIn("Keep the creative structure and localize the setting.", analysis_user_text)
            self.assertEqual(len(h3.create_calls), 1)
            self.assertEqual(
                [item["type"] for item in h3.create_calls[0][0]],
                ["text", "video_url", "image_url"],
            )
            h3_prompt = h3.create_calls[0][0][0]["text"]
            self.assertIn("BEGIN DOUBAO LOCALIZATION PLAN", h3_prompt)
            self.assertIn("Riyadh office and Arabic storefront signage", h3_prompt)
            self.assertIn("Arabic text", h3_prompt)
            self.assertIn("Translate or redraw existing visible text", h3_prompt)
            self.assertIn("stable Arabic speech", h3_prompt)
            checkpoint = json.loads(
                (root / "work" / result.job_id / "checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertEqual(checkpoint["pipeline_version"], 7)
            self.assertEqual(checkpoint["provider"], "minimax_h3")
            self.assertEqual(
                [item["node"] for item in checkpoint["node_executions"]],
                ["doubao", "seedream", "h3"],
            )
            self.assertTrue(
                (root / "work" / result.job_id / "json/nodes/seedream/shot_shot_001/attempt_001/reference.png").is_file()
            )
            self.assertTrue(
                (root / "work" / result.job_id / "json" / "doubao_h3_prompt.txt").is_file()
            )
            self.assertEqual(checkpoint["h3_segments"][0]["normalized_duration_seconds"], 6)

    def test_manual_mode_pauses_after_doubao_until_approved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            ark = FakeArk(self._doubao_payload())
            h3 = FakeH3()
            with patch("core.h3_pipeline.ffprobe.probe", return_value=_info(source, 5.75)), patch(
                "core.h3_pipeline.normalize_video", side_effect=self._normalize
            ), patch("core.h3_pipeline.extract_frame_at", side_effect=self._extract_frame), patch(
                "core.h3_pipeline.download", side_effect=self._download
            ):
                pipeline = VideoLocalizationPipeline(
                    self._config(root),
                    ark_client=ark,
                    minimax_client=h3,
                    uguu_client=FakeUguu(),
                )
                waiting = pipeline.run(self._spec(source), skip_preflight=True)
                self.assertEqual(waiting.stage.value, "waiting_for_approval")
                self.assertEqual(waiting.action_required, "approve_doubao")
                self.assertEqual(len(h3.create_calls), 0)
                reference_waiting = pipeline.approve_doubao(waiting.job_id)
                self.assertEqual(reference_waiting.stage.value, "waiting_for_reference_approval")
                self.assertEqual(reference_waiting.action_required, "approve_seedream")
                self.assertEqual(len(ark.image_calls), 1)
                approved = pipeline.approve_seedream(waiting.job_id)

            self.assertEqual(approved.stage.value, "completed")
            self.assertEqual(len(ark.chat_calls), 1)
            self.assertEqual(len(h3.create_calls), 1)

    def test_failed_doubao_requires_explicit_retry_and_preserves_attempt_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            ark = FakeArk(self._doubao_payload(), fail_first=True)
            h3 = FakeH3()
            with patch("core.h3_pipeline.ffprobe.probe", return_value=_info(source, 5.0)), patch(
                "core.h3_pipeline.normalize_video", side_effect=self._normalize
            ), patch("core.h3_pipeline.extract_frame_at", side_effect=self._extract_frame), patch(
                "core.h3_pipeline.download", side_effect=self._download
            ):
                pipeline = VideoLocalizationPipeline(
                    self._config(root),
                    ark_client=ark,
                    minimax_client=h3,
                    uguu_client=FakeUguu(),
                )
                with self.assertRaises(ProviderError):
                    pipeline.run(self._spec(source), execution_mode="auto", skip_preflight=True)
                job_id = pipeline.history_store.list_entries()[0].job_id
                failed_checkpoint = json.loads(
                    (root / "work" / job_id / "checkpoint.json").read_text(encoding="utf-8")
                )
                self.assertEqual(failed_checkpoint["stage"], "failed")
                self.assertEqual(failed_checkpoint["node_executions"][0]["status"], "failed")
                self.assertFalse((root / "work" / job_id / "json/localization_package.json").exists())

                # Re-entering a failed job is read-only; only the explicit
                # retry operation is allowed to make another Doubao call.
                restarted_ark = FakeArk(self._doubao_payload())
                restarted = VideoLocalizationPipeline(
                    self._config(root),
                    ark_client=restarted_ark,
                    minimax_client=h3,
                    uguu_client=FakeUguu(),
                )
                unchanged = restarted.run(
                    self._spec(source), job_id=job_id, skip_preflight=True, execution_mode="auto"
                )
                self.assertEqual(unchanged.stage.value, "failed")
                self.assertEqual(len(restarted_ark.chat_calls), 0)

                result = pipeline.retry_doubao(job_id)

            self.assertEqual(result.stage.value, "completed")
            self.assertEqual(len(ark.chat_calls), 2)
            self.assertEqual(len(ark.image_calls), 1)
            self.assertEqual(len(h3.create_calls), 1)
            job_dir = root / "work" / job_id
            checkpoint = json.loads((job_dir / "checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [node["node"] for node in checkpoint["node_executions"]],
                ["doubao", "doubao", "seedream", "h3"],
            )
            self.assertEqual(
                [node["status"] for node in checkpoint["node_executions"][:2]],
                ["failed", "completed"],
            )
            self.assertEqual(
                checkpoint["node_executions"][0]["provider_calls"][0]["request_id"],
                "doubao-request-1",
            )
            self.assertEqual(
                checkpoint["node_executions"][1]["provider_calls"][0]["request_id"],
                "doubao-request-2",
            )
            self.assertTrue((job_dir / "json/nodes/doubao/attempt_001/failure.json").is_file())
            self.assertTrue((job_dir / "json/nodes/doubao/attempt_002/package.json").is_file())
            self.assertTrue((job_dir / "json/nodes/doubao/attempt_002/h3_prompt.txt").is_file())
            self.assertTrue(
                (job_dir / "json/nodes/seedream/shot_shot_001/attempt_001/response.json").is_file()
            )
            self.assertTrue((job_dir / "json/localization_package.json").is_file())
            self.assertTrue((job_dir / "json/doubao_h3_prompt.txt").is_file())

    def test_restart_recovers_valid_doubao_package_without_calling_doubao_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            original_ark = FakeArk(self._doubao_payload())
            h3 = FakeH3()
            with patch("core.h3_pipeline.ffprobe.probe", return_value=_info(source, 5.0)), patch(
                "core.h3_pipeline.normalize_video", side_effect=self._normalize
            ), patch("core.h3_pipeline.extract_frame_at", side_effect=self._extract_frame):
                pipeline = VideoLocalizationPipeline(
                    self._config(root),
                    ark_client=original_ark,
                    minimax_client=h3,
                    uguu_client=FakeUguu(),
                )
                waiting = pipeline.run(self._spec(source), skip_preflight=True)
                checkpoint_path = root / "work" / waiting.job_id / "checkpoint.json"
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                checkpoint["stage"] = "analyzing"
                checkpoint["approval_status"] = "not_required"
                checkpoint["pending_approval"] = None
                checkpoint["node_executions"][0]["status"] = "running"
                checkpoint["node_executions"][0]["finished_at"] = None
                checkpoint_path.write_text(
                    json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8"
                )

                restarted_ark = FakeArk(self._doubao_payload())
                restarted = VideoLocalizationPipeline(
                    self._config(root),
                    ark_client=restarted_ark,
                    minimax_client=h3,
                    uguu_client=FakeUguu(),
                )
                recovered = restarted.run(
                    self._spec(source),
                    job_id=waiting.job_id,
                    execution_mode="auto",
                    skip_preflight=True,
                )

            self.assertEqual(recovered.stage.value, "waiting_for_approval")
            self.assertEqual(recovered.action_required, "approve_doubao")
            self.assertEqual(len(original_ark.chat_calls), 1)
            self.assertEqual(len(restarted_ark.chat_calls), 0)
            checkpoint = json.loads(
                (root / "work" / waiting.job_id / "checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertEqual(checkpoint["node_executions"][0]["status"], "completed")
            self.assertEqual(checkpoint["stage"], "waiting_for_approval")

    def test_restart_does_not_recover_stale_package_after_new_analysis_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            ark = FakeArk(self._doubao_payload())
            h3 = FakeH3()
            with patch("core.h3_pipeline.ffprobe.probe", return_value=_info(source, 5.0)), patch(
                "core.h3_pipeline.normalize_video", side_effect=self._normalize
            ), patch("core.h3_pipeline.extract_frame_at", side_effect=self._extract_frame), patch(
                "core.h3_pipeline.download", side_effect=self._download
            ):
                pipeline = VideoLocalizationPipeline(
                    self._config(root),
                    ark_client=ark,
                    minimax_client=h3,
                    uguu_client=FakeUguu(),
                )
                waiting = pipeline.run(self._spec(source), skip_preflight=True)
                checkpoint_path = root / "work" / waiting.job_id / "checkpoint.json"
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                checkpoint["stage"] = "failed"
                checkpoint["approval_status"] = "not_required"
                checkpoint["pending_approval"] = None
                checkpoint["node_executions"].append(
                    {
                        "node": "doubao",
                        "attempt": 2,
                        "status": "failed",
                        "provider": "doubao",
                        "segment_index": None,
                        "started_at": datetime.now(timezone.utc).isoformat(),
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "request_ids": ["doubao-request-2"],
                        "task_id": None,
                        "input_artifacts": [],
                        "output_artifacts": [],
                        "provider_calls": [],
                        "error": {"error_code": "ANALYSIS_FAILED", "message": "failed"},
                    }
                )
                checkpoint_path.write_text(
                    json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8"
                )

                restarted_ark = FakeArk(self._doubao_payload())
                restarted = VideoLocalizationPipeline(
                    self._config(root),
                    ark_client=restarted_ark,
                    minimax_client=h3,
                    uguu_client=FakeUguu(),
                )
                result = restarted.run(
                    self._spec(source),
                    job_id=waiting.job_id,
                    skip_preflight=True,
                    execution_mode="auto",
                )

            self.assertEqual(result.stage.value, "failed")
            self.assertEqual(len(restarted_ark.chat_calls), 0)

    def test_default_prompt_requires_full_scene_transformation(self) -> None:
        prompt = build_transformation_prompt(
            target_language="ar",
            target_region="Saudi Arabia",
            target_locale="ar-SA",
        )
        self.assertIn("not a dubbing-only", prompt)
        self.assertIn("Mandatory full-scene transformation", prompt)
        self.assertIn("PRIORITY 1", prompt)
        self.assertIn("Do not output a character-only edit", prompt)
        self.assertIn("No readable source-language letters may remain", prompt)
        self.assertIn("Keep the transformed people, environment", prompt)

    def test_successful_output_with_container_drift_is_normalized_locally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            h3 = FakeH3()
            ark = FakeArk(self._doubao_payload())

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
            ), patch("core.h3_pipeline.extract_frame_at", side_effect=self._extract_frame), patch(
                "core.h3_pipeline.download", side_effect=self._download
            ):
                result = VideoLocalizationPipeline(
                    self._config(root),
                    ark_client=ark,
                    minimax_client=h3,
                    uguu_client=FakeUguu(),
                ).run(self._spec(source), execution_mode="auto", skip_preflight=True)

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
            ark = FakeArk(self._doubao_payload(16000))
            uguu = FakeUguu()

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
            ), patch("core.h3_pipeline.extract_frame_at", side_effect=self._extract_frame), patch(
                "core.h3_pipeline.download", side_effect=self._download
            ), patch("core.h3_pipeline.concat_videos", side_effect=fake_concat):
                pipeline = VideoLocalizationPipeline(
                    self._config(root),
                    ark_client=ark,
                    minimax_client=h3,
                    uguu_client=uguu,
                )
                waiting = pipeline.run(
                    self._spec(master), execution_mode="auto", skip_preflight=True
                )
                self.assertEqual(waiting.stage.value, "waiting_for_segments")
                self.assertEqual(len(h3.create_calls), 0)
                self.assertEqual(waiting.next_segment_index, 1)
                first_result = pipeline.append_segment(waiting.job_id, first)
                self.assertEqual(first_result.stage.value, "waiting_for_next_segment")
                second_result = pipeline.append_segment(waiting.job_id, second)
                self.assertEqual(second_result.stage.value, "waiting_for_next_segment")
                final = pipeline.finalize(waiting.job_id)

            self.assertEqual(final.stage.value, "completed")
            self.assertEqual(len(ark.image_calls), 2)
            self.assertEqual(len(ark.image_calls[0][0]), 1)
            self.assertEqual(len(ark.image_calls[1][0]), 2)
            self.assertEqual(ark.image_calls[1][0][1], uguu.assets[2].remote_url)
            checkpoint = json.loads(
                (root / "work" / waiting.job_id / "checkpoint.json").read_text(
                    encoding="utf-8"
                )
            )
            second_reference = checkpoint["seedream_references"][1]
            self.assertEqual(
                second_reference["attempts"][0]["continuity_reference_shot_id"],
                "shot_001",
            )
            self.assertTrue(
                second_reference["attempts"][0]["continuity_reference_artifact"].replace(
                    "\\", "/"
                ).endswith("shot_shot_001/attempt_001/reference.png")
            )
            self.assertEqual(
                checkpoint["node_executions"][2]["input_artifacts"][-1],
                second_reference["attempts"][0]["continuity_reference_artifact"],
            )
            self.assertEqual(len(h3.create_calls), 2)
            second_content = h3.create_calls[1][0]
            self.assertEqual(
                [item["type"] for item in second_content],
                ["text", "video_url", "image_url"],
            )
            self.assertEqual(
                [item.get("role") for item in second_content[1:]],
                ["reference_video", "reference_image"],
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
            ark = FakeArk(self._doubao_payload(16000))

            def probe(path, **kwargs):
                return _info(Path(path), 16.5 if Path(path).name == "source_master.mp4" else 4.0)

            with patch("core.h3_pipeline.ffprobe.probe", side_effect=probe), patch(
                "core.h3_pipeline.normalize_video", side_effect=self._normalize
            ), patch("core.h3_pipeline.extract_frame_at", side_effect=self._extract_frame), patch(
                "core.h3_pipeline.download", side_effect=self._download
            ):
                pipeline = VideoLocalizationPipeline(
                    self._config(root),
                    ark_client=ark,
                    minimax_client=h3,
                    uguu_client=FakeUguu(),
                )
                waiting = pipeline.run(
                    self._spec(master), execution_mode="auto", skip_preflight=True
                )
                pipeline.append_segment(waiting.job_id, first)
                pipeline.append_segment(waiting.job_id, second)

            second_content = h3.create_calls[1][0]
            self.assertEqual(
                [item["type"] for item in second_content],
                ["text", "video_url", "image_url"],
            )
            self.assertEqual(
                [item.get("role") for item in second_content[1:]],
                ["reference_video", "reference_image"],
            )

    def test_failed_segment_retry_creates_new_task_without_repeating_any_other_step(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            h3 = FakeH3(fail_first=True)
            ark = FakeArk(self._doubao_payload())
            with patch("core.h3_pipeline.ffprobe.probe", return_value=_info(source, 5.0)), patch(
                "core.h3_pipeline.normalize_video", side_effect=self._normalize
            ), patch("core.h3_pipeline.extract_frame_at", side_effect=self._extract_frame), patch(
                "core.h3_pipeline.download", side_effect=self._download
            ):
                pipeline = VideoLocalizationPipeline(
                    self._config(root),
                    ark_client=ark,
                    minimax_client=h3,
                    uguu_client=FakeUguu(),
                )
                with self.assertRaises(ProviderError):
                    pipeline.run(self._spec(source), execution_mode="auto", skip_preflight=True)
                # Obtain the only job ID from the persisted history without
                # relying on a provider response or a GUI selection.
                entries = pipeline.history_store.list_entries()
                job_id = entries[0].job_id
                result = pipeline.retry_segment(job_id)

            self.assertEqual(result.stage.value, "completed")
            self.assertEqual(len(h3.create_calls), 2)
            checkpoint = json.loads(
                (root / "work" / job_id / "checkpoint.json").read_text(encoding="utf-8")
            )
            attempts = checkpoint["h3_segments"][0]["attempts"]
            self.assertEqual([item["status"] for item in attempts], ["failed", "completed"])
            self.assertTrue((root / "work" / job_id / "json/nodes/h3/segment_001/attempt_001/failure.json").is_file())

    def test_timeout_keeps_task_id_and_continue_after_restart_does_not_create_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            h3 = FakeH3(timeout_first=True)
            ark = FakeArk(self._doubao_payload())
            with patch("core.h3_pipeline.ffprobe.probe", return_value=_info(source, 5.0)), patch(
                "core.h3_pipeline.normalize_video", side_effect=self._normalize
            ), patch("core.h3_pipeline.extract_frame_at", side_effect=self._extract_frame), patch(
                "core.h3_pipeline.download", side_effect=self._download
            ):
                pipeline = VideoLocalizationPipeline(
                    self._config(root),
                    ark_client=ark,
                    minimax_client=h3,
                    uguu_client=FakeUguu(),
                )
                with self.assertRaises(ProviderError):
                    pipeline.run(self._spec(source), execution_mode="auto", skip_preflight=True)
                job_id = pipeline.history_store.list_entries()[0].job_id
                restarted = VideoLocalizationPipeline(
                    self._config(root),
                    ark_client=ark,
                    minimax_client=h3,
                    uguu_client=FakeUguu(),
                )
                result = restarted.continue_segment(job_id)

            self.assertEqual(result.stage.value, "completed")
            self.assertEqual(len(ark.chat_calls), 1)
            self.assertEqual(len(h3.create_calls), 1)
            self.assertEqual(h3.wait_calls, ["h3-task-1", "h3-task-1"])

    def test_seedream_failure_requires_explicit_retry_and_never_repeats_doubao(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            ark = FakeArk(self._doubao_payload(), image_fail_first=True)
            h3 = FakeH3()
            with patch("core.h3_pipeline.ffprobe.probe", return_value=_info(source, 5.0)), patch(
                "core.h3_pipeline.normalize_video", side_effect=self._normalize
            ), patch("core.h3_pipeline.extract_frame_at", side_effect=self._extract_frame), patch(
                "core.h3_pipeline.download", side_effect=self._download
            ):
                pipeline = VideoLocalizationPipeline(
                    self._config(root),
                    ark_client=ark,
                    minimax_client=h3,
                    uguu_client=FakeUguu(),
                )
                waiting = pipeline.run(self._spec(source), skip_preflight=True)
                with self.assertRaises(ProviderError):
                    pipeline.approve_doubao(waiting.job_id)
                job_id = waiting.job_id
                failed = pipeline.run(
                    self._spec(source),
                    job_id=job_id,
                    skip_preflight=True,
                )
                self.assertEqual(failed.stage.value, "failed")
                self.assertEqual(len(ark.chat_calls), 1)
                self.assertEqual(len(ark.image_calls), 1)

                reference_waiting = pipeline.retry_seedream(job_id)
                self.assertEqual(reference_waiting.stage.value, "waiting_for_reference_approval")
                self.assertEqual(len(ark.chat_calls), 1)
                self.assertEqual(len(ark.image_calls), 2)
                completed = pipeline.approve_seedream(job_id)

            self.assertEqual(completed.stage.value, "completed")
            job_dir = root / "work" / job_id
            self.assertTrue(
                (job_dir / "json/nodes/seedream/shot_shot_001/attempt_001/failure.json").is_file()
            )
            self.assertTrue(
                (job_dir / "json/nodes/seedream/shot_shot_001/attempt_002/reference.png").is_file()
            )

    def test_user_reference_images_are_rejected_by_active_v7_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            user_reference = root / "old-reference.png"
            source.write_bytes(b"source")
            user_reference.write_bytes(b"reference")
            ark = FakeArk(self._doubao_payload())
            pipeline = VideoLocalizationPipeline(
                self._config(root),
                ark_client=ark,
                minimax_client=FakeH3(),
                uguu_client=FakeUguu(),
            )
            with self.assertRaises(ValidationError):
                pipeline.run(
                    self._spec(source, [user_reference]),
                    skip_preflight=True,
                )
            self.assertEqual(len(ark.chat_calls), 0)

    def test_segment_covering_more_than_nine_storyboard_shots_is_rejected_before_h3(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            payload = self._doubao_payload(5000)
            payload["reference_shots"] = [
                {
                    "shot_id": f"shot_{index:03d}",
                    "start_ms": index * 500,
                    "end_ms": (index + 1) * 500,
                    "keyframe_ms": index * 500 + 100,
                    "character_ids": ["speaker_1"],
                    "continuity_group": "main-scene",
                    "scene_description": "Target-region localized street scene.",
                    "replacement_requirements": ["replace the complete visible environment"],
                    "preserve_requirements": ["preserve the same composition and action timing"],
                    "seedream_prompt": "Rebuild the complete localized scene from this keyframe.",
                }
                for index in range(10)
            ]
            ark = FakeArk(payload)
            h3 = FakeH3()
            with patch("core.h3_pipeline.ffprobe.probe", return_value=_info(source, 5.0)), patch(
                "core.h3_pipeline.normalize_video", side_effect=self._normalize
            ), patch("core.h3_pipeline.extract_frame_at", side_effect=self._extract_frame), patch(
                "core.h3_pipeline.download", side_effect=self._download
            ):
                pipeline = VideoLocalizationPipeline(
                    self._config(root),
                    ark_client=ark,
                    minimax_client=h3,
                    uguu_client=FakeUguu(),
                )
                with self.assertRaises(ValidationError):
                    pipeline.run(self._spec(source), execution_mode="auto", skip_preflight=True)

            self.assertEqual(len(ark.chat_calls), 1)
            self.assertEqual(len(ark.image_calls), 10)
            self.assertEqual(len(h3.create_calls), 0)

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
