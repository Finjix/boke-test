from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.models import JobContext, JobSpec, PipelineStage
from utils.artifacts import write_json
from utils.history import HistoryStore


class HistoryStoreTests(unittest.TestCase):
    def _context(self, root: Path, job_id: str = "job-test") -> JobContext:
        job_dir = root / "work" / job_id
        job_dir.mkdir(parents=True)
        spec = JobSpec(
            input_video=root / "input.mp4",
            target_language="ar",
            target_region="Gulf",
            target_locale="ar-SA",
        )
        return JobContext(
            job_id=job_id,
            job_dir=job_dir,
            spec=spec,
            created_at="2026-09-02T00:00:00+00:00",
            updated_at="2026-09-02T00:01:00+00:00",
            stage=PipelineStage.WAITING_FOR_APPROVAL,
        )

    def test_corrupt_index_is_rebuilt_from_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = self._context(root)
            write_json(context.job_dir / "checkpoint.json", context.model_dump(mode="json"))
            store = HistoryStore(root / "work")
            (root / "work" / "history.json").write_text("not-json", encoding="utf-8")

            entries = store.list_entries()

            self.assertEqual([entry.job_id for entry in entries], ["job-test"])
            repaired = json.loads((root / "work" / "history.json").read_text(encoding="utf-8"))
            self.assertEqual(repaired["version"], 1)
            self.assertEqual(repaired["jobs"][0]["stage"], "waiting_for_approval")

    def test_legacy_checkpoint_is_visible_but_not_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_dir = root / "work" / "legacy-job"
            legacy_dir.mkdir(parents=True)
            write_json(
                legacy_dir / "checkpoint.json",
                {
                    "pipeline_version": 3,
                    "job_id": "legacy-job",
                    "stage": "completed",
                    "spec": {"input_video": "old.mp4", "target_locale": "ar-SA"},
                },
            )

            entries = HistoryStore(root / "work").list_entries()

            self.assertEqual(len(entries), 1)
            self.assertFalse(entries[0].compatible)
            self.assertEqual(entries[0].status, "incompatible")

    def test_delete_removes_only_the_selected_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._context(root, "job-first")
            second = self._context(root, "job-second")
            write_json(first.job_dir / "checkpoint.json", first.model_dump(mode="json"))
            write_json(second.job_dir / "checkpoint.json", second.model_dump(mode="json"))
            store = HistoryStore(root / "work")

            store.delete("job-first")

            self.assertFalse(first.job_dir.exists())
            self.assertTrue(second.job_dir.exists())
            self.assertEqual([entry.job_id for entry in store.list_entries()], ["job-second"])
