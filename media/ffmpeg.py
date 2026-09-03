"""Small FFmpeg operations used by the single-video pipeline."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

from config import MINIMAX_GENERATION_MIN_DURATION_SECONDS, MINIMAX_GENERATION_MAX_DURATION_SECONDS
from utils.errors import MediaCommandError, ValidationError


def resolve_ffmpeg_bin(ffprobe_bin: str = "ffprobe", ffmpeg_bin: str | None = None) -> str:
    if ffmpeg_bin:
        return ffmpeg_bin
    probe_path = Path(ffprobe_bin).expanduser()
    if probe_path.is_file():
        sibling = probe_path.with_name("ffmpeg" + probe_path.suffix)
        if sibling.is_file():
            return str(sibling)
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
        raise MediaCommandError(f"ffmpeg 不存在: {command[0]}", command=command) from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaCommandError("ffmpeg 超时", command=command) from exc
    if completed.returncode != 0:
        raise MediaCommandError(
            "ffmpeg 处理失败",
            command=command,
            stderr=completed.stderr,
        )


def _temporary_path(destination: Path) -> Path:
    return destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.tmp{destination.suffix}"
    )


def _finish_atomic(temporary: Path, destination: Path) -> Path:
    if not temporary.is_file() or temporary.stat().st_size <= 0:
        raise MediaCommandError(f"ffmpeg 未产生输出: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary.replace(destination)
    return destination


def normalize_video(
    source: Path,
    destination: Path,
    *,
    duration_seconds: int,
    source_duration_seconds: float | None = None,
    include_audio: bool = True,
    ffprobe_bin: str = "ffprobe",
    ffmpeg_bin: str | None = None,
    timeout: float = 600.0,
) -> Path:
    """Convert a source to H.264 MP4 and an integer H3 duration."""

    source = Path(source)
    destination = Path(destination)
    if not source.is_file():
        raise ValidationError("源视频不存在")
    if not (
        isinstance(duration_seconds, int)
        and MINIMAX_GENERATION_MIN_DURATION_SECONDS
        <= duration_seconds
        <= MINIMAX_GENERATION_MAX_DURATION_SECONDS
    ):
        raise ValidationError("H3 时长必须为 4–15 秒整数")
    if source_duration_seconds is None:
        from media import ffprobe

        info = ffprobe.probe(
            source,
            ffprobe_bin=ffprobe_bin,
            timeout=min(timeout, 120.0),
        )
        source_duration_seconds = info.duration
        include_audio = info.has_audio
    if source_duration_seconds <= 0:
        raise ValidationError("源视频时长无效")

    padding = max(0.0, duration_seconds - float(source_duration_seconds))
    video_filter = (
        f"tpad=stop_mode=clone:stop_duration={padding:.6f},"
        f"trim=duration={duration_seconds},setpts=PTS-STARTPTS"
    )
    ffmpeg = resolve_ffmpeg_bin(ffprobe_bin, ffmpeg_bin)
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
        "-vf",
        video_filter,
        "-t",
        str(duration_seconds),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
    ]
    if include_audio:
        command.extend(
            [
                "-map",
                "0:a:0?",
                "-af",
                f"apad,atrim=duration={duration_seconds},asetpts=PTS-STARTPTS",
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-ac",
                "2",
            ]
        )
    else:
        command.append("-an")
    command.extend(["-movflags", "+faststart", str(temporary)])
    try:
        _run(command, timeout=timeout)
        return _finish_atomic(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
