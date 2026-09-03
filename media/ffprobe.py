"""Small ffprobe metadata helper."""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from utils.errors import MediaCommandError


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration: float
    streams: list[dict[str, Any]]
    format_name: str
    raw: dict[str, Any]

    @property
    def has_video(self) -> bool:
        return any(stream.get("codec_type") == "video" for stream in self.streams)

    @property
    def has_audio(self) -> bool:
        return any(stream.get("codec_type") == "audio" for stream in self.streams)


def probe(path: Path, *, ffprobe_bin: str = "ffprobe", timeout: float = 60.0) -> MediaInfo:
    path = Path(path)
    if not path.is_file():
        raise MediaCommandError(f"媒体文件不存在: {path}")
    command = [
        ffprobe_bin,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
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
        raise MediaCommandError(f"ffprobe 不存在: {ffprobe_bin}", command=command) from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaCommandError("ffprobe 超时", command=command) from exc
    if completed.returncode != 0:
        raise MediaCommandError("ffprobe 读取媒体失败", command=command, stderr=completed.stderr)
    try:
        raw = json.loads(completed.stdout)
        duration = float(raw["format"]["duration"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise MediaCommandError("ffprobe 返回信息无效", command=command) from exc
    if not math.isfinite(duration) or duration <= 0:
        raise MediaCommandError("媒体时长无效", command=command)
    streams = raw.get("streams") if isinstance(raw.get("streams"), list) else []
    return MediaInfo(
        path=path,
        duration=duration,
        streams=streams,
        format_name=str(raw.get("format", {}).get("format_name", "")),
        raw=raw,
    )


def duration(path: Path, *, ffprobe_bin: str = "ffprobe", timeout: float = 60.0) -> float:
    return probe(path, ffprobe_bin=ffprobe_bin, timeout=timeout).duration
