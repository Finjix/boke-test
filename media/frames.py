"""Frame extraction helpers."""

from __future__ import annotations

from pathlib import Path

from core.speaker import SpeakerAnchor
from media.ffmpeg import extract_frame


def extract_anchor_frames(
    video_path: Path,
    anchors: list[SpeakerAnchor],
    frames_dir: Path,
    *,
    ffmpeg_bin: str = "ffmpeg",
) -> dict[str, list[Path]]:
    frames: dict[str, list[Path]] = {}
    for index, anchor in enumerate(anchors, start=1):
        for label, timestamp in (
            ("start", anchor.start),
            ("mid", anchor.middle),
            ("end", anchor.end),
        ):
            path = frames_dir / f"{anchor.speaker_id}_{index:03d}_{label}.jpg"
            extract_frame(video_path, timestamp, path, ffmpeg_bin=ffmpeg_bin)
            frames.setdefault(anchor.speaker_id, []).append(path)
    return frames
