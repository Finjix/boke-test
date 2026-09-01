"""FFmpeg operations used by the final media assembly."""

from __future__ import annotations

import math
import subprocess
from pathlib import Path

from utils.errors import MediaCommandError
from video_config import AUDIO_SAMPLE_RATE


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


def extract_frame(
    video_path: Path,
    timestamp: float,
    output_path: Path,
    *,
    ffmpeg_bin: str = "ffmpeg",
    timeout: float = 120.0,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-y",
            "-ss",
            f"{max(0.0, timestamp):.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(output_path),
        ],
        ffmpeg_bin=ffmpeg_bin,
        timeout=timeout,
    )
    return output_path


def adjust_audio_tempo(
    input_path: Path,
    output_path: Path,
    factor: float,
    *,
    ffmpeg_bin: str = "ffmpeg",
    timeout: float = 300.0,
) -> Path:
    if not math.isfinite(factor) or factor <= 0:
        raise ValueError("atempo factor must be positive")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-y",
            "-i",
            str(input_path),
            "-filter:a",
            f"atempo={factor:.8f}",
            "-ar",
            str(AUDIO_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ],
        ffmpeg_bin=ffmpeg_bin,
        timeout=timeout,
    )
    return output_path


def mix_audio(
    background_path: Path,
    voice_path: Path,
    output_path: Path,
    *,
    voice_gain_db: float = 0.0,
    ffmpeg_bin: str = "ffmpeg",
    timeout: float = 300.0,
) -> Path:
    if not 0 <= voice_gain_db <= 3:
        raise ValueError("voice gain must be between 0 and 3 dB")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    gain = f"volume={voice_gain_db:.3f}dB"
    run_ffmpeg(
        [
            "-y",
            "-i",
            str(background_path),
            "-i",
            str(voice_path),
            "-filter_complex",
            (
                f"[0:a]aresample={AUDIO_SAMPLE_RATE}[bg];"
                f"[1:a]aresample={AUDIO_SAMPLE_RATE},{gain}[voice];"
                "[bg][voice]amix=inputs=2:duration=longest:normalize=0[a]"
            ),
            "-map",
            "[a]",
            "-ar",
            str(AUDIO_SAMPLE_RATE),
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
    audio_path: Path,
    output_path: Path,
    *,
    ffmpeg_bin: str = "ffmpeg",
    timeout: float = 300.0,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
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
