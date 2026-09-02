"""FFmpeg operations used by the localization pipeline."""

from __future__ import annotations

import subprocess
from pathlib import Path

from utils.errors import MediaCommandError


def run_ffmpeg(
    args: list[str],
    *,
    ffmpeg_bin: str = "ffmpeg",
    timeout: float = 300.0,
) -> subprocess.CompletedProcess[str]:
    command = [ffmpeg_bin, *args]
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
        raise MediaCommandError(f"ffmpeg not found: {ffmpeg_bin}", command=command) from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaCommandError("ffmpeg timed out", command=command) from exc
    if completed.returncode != 0:
        raise MediaCommandError(
            "ffmpeg command failed",
            command=command,
            stderr=completed.stderr,
        )
    return completed


def extract_audio(
    video_path: Path,
    output_path: Path,
    *,
    ffmpeg_bin: str = "ffmpeg",
    timeout: float = 300.0,
) -> Path:
    """Extract the complete first audio stream without audio effects."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-y",
            "-i",
            str(video_path),
            "-map",
            "0:a:0",
            "-vn",
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ],
        ffmpeg_bin=ffmpeg_bin,
        timeout=timeout,
    )
    return output_path


def mux_video(
    video_path: Path,
    localized_audio_path: Path,
    output_path: Path,
    *,
    ffmpeg_bin: str = "ffmpeg",
    timeout: float = 300.0,
) -> Path:
    """Mux Seedance video with the exact localized audio source."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(localized_audio_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "256k",
            "-shortest",
            str(output_path),
        ],
        ffmpeg_bin=ffmpeg_bin,
        timeout=timeout,
    )
    return output_path
