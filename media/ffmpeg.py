"""Small ffmpeg operations used by the H3 segmented workflow."""

from __future__ import annotations

import math
import subprocess
import uuid
from pathlib import Path

from config import MINIMAX_GENERATION_MIN_DURATION_SECONDS
from media import ffprobe
from utils.errors import MediaCommandError, ValidationError


def resolve_ffmpeg_bin(ffprobe_bin: str = "ffprobe") -> str:
    probe_path = Path(ffprobe_bin).expanduser()
    if probe_path.is_file():
        sibling = probe_path.with_name("ffmpeg" + probe_path.suffix)
        if sibling.is_file():
            return str(sibling)
    bundled = probe_path.parent / ("ffmpeg" + probe_path.suffix)
    if bundled.is_file():
        return str(bundled)
    return "ffmpeg"


def _run(command: list[str], *, timeout: float) -> None:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise MediaCommandError(
            f"ffmpeg not found: {command[0]}", command=command
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaCommandError("ffmpeg timed out", command=command) from exc
    if completed.returncode != 0:
        raise MediaCommandError(
            "ffmpeg failed",
            command=command,
            stderr=completed.stderr,
        )


def _temporary_path(destination: Path) -> Path:
    return destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp{destination.suffix}")


def _finish_atomic(temporary: Path, destination: Path) -> Path:
    if not temporary.is_file() or temporary.stat().st_size <= 0:
        raise MediaCommandError(f"ffmpeg produced no output file: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary.replace(destination)
    return destination


def normalize_video(
    source: Path,
    destination: Path,
    *,
    duration_seconds: int,
    source_duration_seconds: float | None = None,
    ffprobe_bin: str = "ffprobe",
    ffmpeg_bin: str | None = None,
    timeout: float = 600.0,
) -> Path:
    """Re-encode one H3 source slice to an integer duration.

    H3 accepts only integer output durations. If rounding makes the requested
    duration longer than the source, the final video frame and audio are padded
    locally; no provider call is made for this normalization.
    """

    source = Path(source)
    destination = Path(destination)
    if not source.is_file():
        raise ValidationError(f"source video does not exist: {source}")
    if not isinstance(duration_seconds, int) or not MINIMAX_GENERATION_MIN_DURATION_SECONDS <= duration_seconds <= 15:
        raise ValidationError("normalized H3 duration must be an integer from 4 to 15")
    if source_duration_seconds is None:
        source_duration_seconds = ffprobe.probe(
            source,
            ffprobe_bin=ffprobe_bin,
            timeout=min(timeout, 120.0),
        ).duration
    if source_duration_seconds <= 0:
        raise ValidationError("source video duration must be positive")
    padding = max(0.0, duration_seconds - float(source_duration_seconds))
    video_filter = f"tpad=stop_mode=clone:stop_duration={padding:.6f},trim=duration={duration_seconds},setpts=PTS-STARTPTS"
    audio_filter = f"apad,atrim=duration={duration_seconds},asetpts=PTS-STARTPTS"
    ffmpeg = ffmpeg_bin or resolve_ffmpeg_bin(ffprobe_bin)
    temporary = _temporary_path(destination)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-vf",
        video_filter,
        "-af",
        audio_filter,
        "-t",
        str(duration_seconds),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        str(temporary),
    ]
    try:
        _run(command, timeout=timeout)
        return _finish_atomic(temporary, destination)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _concat_manifest(sources: list[Path], manifest: Path) -> None:
    lines = ["ffconcat version 1.0"]
    for source in sources:
        # The concat demuxer accepts forward slashes and single-quoted paths.
        value = str(Path(source).resolve()).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{value}'")
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")


def concat_videos(
    sources: list[Path],
    destination: Path,
    *,
    target_duration_seconds: float | None = None,
    ffprobe_bin: str = "ffprobe",
    ffmpeg_bin: str | None = None,
    timeout: float = 900.0,
) -> Path:
    """Concatenate completed H3 outputs into one local video."""

    if not sources:
        raise ValidationError("at least one completed segment is required")
    source_paths = [Path(item) for item in sources]
    missing = [str(path) for path in source_paths if not path.is_file()]
    if missing:
        raise ValidationError(f"segment output is missing: {missing[0]}")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = destination.parent / f".concat.{uuid.uuid4().hex}.txt"
    temporary = _temporary_path(destination)
    ffmpeg = ffmpeg_bin or resolve_ffmpeg_bin(ffprobe_bin)
    _concat_manifest(source_paths, manifest)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(manifest),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
    ]
    if target_duration_seconds is not None:
        if target_duration_seconds <= 0 or not math.isfinite(target_duration_seconds):
            raise ValidationError("target concatenated duration must be positive")
        command.extend(["-t", f"{target_duration_seconds:.3f}"])
    command.append(str(temporary))
    try:
        _run(command, timeout=timeout)
        return _finish_atomic(temporary, destination)
    finally:
        try:
            manifest.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
