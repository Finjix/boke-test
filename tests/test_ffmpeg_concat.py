from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media.ffmpeg import concatenate_videos
from utils.errors import ValidationError


class FfmpegConcatTests(unittest.TestCase):
    def test_concatenate_writes_ordered_concat_manifest_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = [root / "first clip.mp4", root / "second.mp4"]
            for source in sources:
                source.write_bytes(b"source")
            destination = root / "output.mp4"
            observed: dict[str, object] = {}

            def fake_run(command: list[str], *, timeout: float) -> None:
                observed["command"] = command
                observed["timeout"] = timeout
                manifest = Path(command[command.index("-i") + 1])
                observed["manifest"] = manifest.read_text(encoding="utf-8")
                Path(command[-1]).write_bytes(b"joined")

            with patch("media.ffmpeg._run", side_effect=fake_run):
                result = concatenate_videos(
                    sources,
                    destination,
                    ffmpeg_bin="fake-ffmpeg",
                    timeout=12,
                )

            self.assertEqual(result, destination)
            self.assertEqual(destination.read_bytes(), b"joined")
            self.assertEqual(
                observed["manifest"],
                "file '" + str(sources[0].resolve()).replace("\\", "/")
                + "'\nfile '"
                + str(sources[1].resolve()).replace("\\", "/")
                + "'\n",
            )
            command = observed["command"]
            self.assertIsInstance(command, list)
            self.assertIn("concat", command)
            self.assertIn("0:v:0", command)
            self.assertEqual(observed["timeout"], 12)

    def test_concatenate_requires_two_existing_videos(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            with self.assertRaisesRegex(ValidationError, "至少选择 2 个视频"):
                concatenate_videos([source], root / "output.mp4")
            with self.assertRaisesRegex(ValidationError, "视频不存在"):
                concatenate_videos(
                    [source, root / "missing.mp4"],
                    root / "output.mp4",
                )


if __name__ == "__main__":
    unittest.main()
