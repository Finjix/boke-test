"""Runtime configuration for the MiniMax H3-Context-IR application."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - dependency is declared in requirements
    load_dotenv = None

from utils.errors import ConfigurationError


FIXED_MINIMAX_H3_MODEL = "MiniMax-H3"
MINIMAX_CN_BASE_URL = "https://api.minimax.cn"
MINIMAX_H3_DEFAULT_RESOLUTION = "768P"
MINIMAX_H3_RESOLUTIONS = frozenset({"768P", "2K"})
MINIMAX_VIDEO_MAX_FILE_MIB = 50
MINIMAX_SEGMENT_MIN_DURATION_SECONDS = 3
MINIMAX_GENERATION_MIN_DURATION_SECONDS = 4
MINIMAX_MAX_DURATION_SECONDS = 15


def _text(env: Mapping[str, str], name: str, default: str = "") -> str:
    return str(env.get(name, default)).strip()


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    value = _text(env, name, str(default))
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    value = _text(env, name, str(default))
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc


def _path_from_env(env: Mapping[str, str], name: str, default: Path, base_dir: Path) -> Path:
    value = _text(env, name, str(default))
    path = Path(value).expanduser()
    return path if path.is_absolute() else base_dir / path


def _tool_from_env(
    env: Mapping[str, str],
    name: str,
    default: str,
    base_dir: Path,
    local_relative: str,
) -> str:
    value = _text(env, name, default)
    local_path = base_dir / local_relative
    if value == default and local_path.is_file():
        return str(local_path)
    path = Path(value).expanduser()
    if path.is_absolute():
        return str(path)
    if "/" in value or "\\" in value or value.startswith("."):
        return str((base_dir / path).resolve())
    return value


@dataclass(frozen=True)
class AppConfig:
    """Validated settings for the two-call MiniMax workflow."""

    minimax_api_key: str = ""
    minimax_base_url: str = MINIMAX_CN_BASE_URL
    minimax_model: str = FIXED_MINIMAX_H3_MODEL
    minimax_task_timeout: int = 7200
    minimax_resolution: str = MINIMAX_H3_DEFAULT_RESOLUTION

    uguu_upload_url: str = "https://uguu.se/upload"
    uguu_max_file_mib: int = MINIMAX_VIDEO_MAX_FILE_MIB
    uguu_expire_hours: int = 3

    http_timeout: float = 180.0
    poll_interval: float = 10.0
    work_dir: Path = Path("work")
    ffprobe_bin: str = "ffprobe"

    def __repr__(self) -> str:  # pragma: no cover - defensive secret hygiene
        return (
            "AppConfig("
            f"minimax_base_url={self.minimax_base_url!r}, "
            f"minimax_model={self.minimax_model!r}, "
            f"minimax_task_timeout={self.minimax_task_timeout!r}, "
            f"minimax_resolution={self.minimax_resolution!r}, "
            f"uguu_upload_url={self.uguu_upload_url!r}, "
            f"uguu_max_file_mib={self.uguu_max_file_mib!r}, "
            f"uguu_expire_hours={self.uguu_expire_hours!r}, "
            f"http_timeout={self.http_timeout!r}, "
            f"poll_interval={self.poll_interval!r}, "
            f"work_dir={str(self.work_dir)!r})"
        )

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        base_dir: Path | None = None,
        load_file: bool = True,
    ) -> "AppConfig":
        root = (base_dir or Path.cwd()).resolve()
        if env is None:
            if load_file and load_dotenv is not None:
                load_dotenv(root / ".env", override=False)
            source: Mapping[str, str] = os.environ
        else:
            source = env

        config = cls(
            minimax_api_key=_text(source, "MINIMAX_API_KEY"),
            minimax_base_url=_text(
                source, "MINIMAX_BASE_URL", MINIMAX_CN_BASE_URL
            ).rstrip("/"),
            minimax_model=FIXED_MINIMAX_H3_MODEL,
            minimax_task_timeout=_int(source, "MINIMAX_TASK_TIMEOUT", 7200),
            minimax_resolution=_text(
                source, "MINIMAX_RESOLUTION", MINIMAX_H3_DEFAULT_RESOLUTION
            ).upper(),
            uguu_upload_url=_text(source, "UGUU_UPLOAD_URL", "https://uguu.se/upload"),
            uguu_max_file_mib=_int(
                source, "UGUU_MAX_FILE_MIB", MINIMAX_VIDEO_MAX_FILE_MIB
            ),
            uguu_expire_hours=_int(source, "UGUU_EXPIRE_HOURS", 3),
            http_timeout=_float(source, "HTTP_TIMEOUT", 180.0),
            poll_interval=_float(source, "POLL_INTERVAL", 10.0),
            work_dir=_path_from_env(source, "WORK_DIR", Path("work"), root),
            ffprobe_bin=_tool_from_env(
                source, "FFPROBE_BIN", "ffprobe", root, "tools/ffmpeg/bin/ffprobe.exe"
            ),
        )
        config.validate_values()
        return config

    def validate_values(self) -> None:
        if self.minimax_model != FIXED_MINIMAX_H3_MODEL:
            raise ConfigurationError(
                f"MINIMAX_MODEL is fixed to {FIXED_MINIMAX_H3_MODEL}"
            )
        if not self.minimax_base_url.startswith(("http://", "https://")):
            raise ConfigurationError("MINIMAX_BASE_URL must be an HTTP(S) URL")
        if self.minimax_task_timeout <= 0:
            raise ConfigurationError("MINIMAX_TASK_TIMEOUT must be positive")
        if self.minimax_resolution not in MINIMAX_H3_RESOLUTIONS:
            raise ConfigurationError(
                f"MINIMAX_RESOLUTION must be one of {sorted(MINIMAX_H3_RESOLUTIONS)}"
            )
        if not self.uguu_upload_url.startswith("https://"):
            raise ConfigurationError("UGUU_UPLOAD_URL must be an HTTPS URL")
        if self.uguu_max_file_mib <= 0 or self.uguu_max_file_mib > MINIMAX_VIDEO_MAX_FILE_MIB:
            raise ConfigurationError(
                f"UGUU_MAX_FILE_MIB must be between 1 and {MINIMAX_VIDEO_MAX_FILE_MIB}"
            )
        if self.uguu_expire_hours <= 0:
            raise ConfigurationError("UGUU_EXPIRE_HOURS must be positive")
        if self.http_timeout <= 0 or self.poll_interval < 0:
            raise ConfigurationError(
                "HTTP_TIMEOUT must be positive and POLL_INTERVAL non-negative"
            )

    def with_overrides(self, **values: object) -> "AppConfig":
        allowed = set(self.__dataclass_fields__)
        unknown = set(values) - allowed
        if unknown:
            raise ConfigurationError(f"Unknown configuration fields: {sorted(unknown)}")
        updated = replace(self, **values)
        updated.validate_values()
        return updated

    def missing_runtime_values(self) -> list[str]:
        return ["MINIMAX_API_KEY"] if not self.minimax_api_key else []

    def missing_h3_runtime_values(self) -> list[str]:
        return self.missing_runtime_values()
