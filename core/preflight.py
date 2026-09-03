"""Cheap local checks that run before any MiniMax task is created."""

from __future__ import annotations

import shutil
from pathlib import Path

from config import AppConfig, FIXED_MINIMAX_H3_MODEL, MINIMAX_H3_RESOLUTIONS
from core.models import JobSpec, PreflightCheck, PreflightReport
from utils.errors import PreflightError


def _executable(value: str) -> bool:
    path = Path(value).expanduser()
    return path.is_file() or shutil.which(value) is not None


def run_preflight(
    config: AppConfig,
    spec: JobSpec,
    *,
    execute_remote_checks: bool = False,
) -> PreflightReport:
    """Validate local prerequisites without making a network request."""

    del execute_remote_checks
    checks: list[PreflightCheck] = []

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append(PreflightCheck(name, passed, detail))

    add("ffprobe", _executable(config.ffprobe_bin), str(config.ffprobe_bin))
    add("ffmpeg", _executable(config.ffmpeg_bin), str(config.ffmpeg_bin))
    add(
        "MINIMAX_API_KEY",
        bool(config.minimax_api_key.strip()),
        "已配置" if config.minimax_api_key.strip() else "未配置",
    )
    add(
        "MINIMAX_MODEL",
        config.minimax_model == FIXED_MINIMAX_H3_MODEL,
        config.minimax_model,
    )
    add(
        "MINIMAX_RESOLUTION",
        config.minimax_resolution in MINIMAX_H3_RESOLUTIONS,
        config.minimax_resolution,
    )
    add("target locale", bool(spec.target_locale), spec.target_locale)
    add("input video", Path(spec.input_video).is_file(), str(spec.input_video))
    if spec.person_image is not None:
        add(
            "person reference",
            Path(spec.person_image).is_file(),
            str(spec.person_image),
        )
    if spec.scene_image is not None:
        add(
            "scene reference",
            Path(spec.scene_image).is_file(),
            str(spec.scene_image),
        )

    return PreflightReport(
        passed=all(check.passed for check in checks if check.fatal),
        checks=tuple(checks),
    )


def require_preflight(report: PreflightReport) -> None:
    if report.passed:
        return
    failed = next((check for check in report.checks if not check.passed), None)
    detail = f": {failed.detail}" if failed is not None and failed.detail else ""
    raise PreflightError(f"启动检查失败{detail}")
