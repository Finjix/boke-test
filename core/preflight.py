"""Startup checks for the two-provider MiniMax workflow."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from api.minimax import MiniMaxClient
from api.uguu import UguuClient
from config import AppConfig
from core.models import JobSpec, PreflightCheck, PreflightReport
from utils.errors import PreflightError
from utils.logger import JobLogger


def _executable(value: str) -> bool:
    path = Path(value).expanduser()
    return path.is_file() or shutil.which(value) is not None


def run_preflight(
    config: AppConfig,
    spec: JobSpec,
    *,
    job_dir: Path | None = None,
    clients: dict[str, Any] | None = None,
    logger: JobLogger | None = None,
    execute_remote_checks: bool = True,
) -> PreflightReport:
    clients = clients or {}
    minimax = clients.get("minimax") or MiniMaxClient(config, logger=logger)
    uploader = clients.get("uguu") or UguuClient(config, logger=logger)
    checks: list[PreflightCheck] = []

    def add(name: str, passed: bool, detail: str, *, fatal: bool = True) -> None:
        check = PreflightCheck(name=name, passed=passed, detail=detail, fatal=fatal)
        checks.append(check)
        if logger:
            logger.emit(
                "INFO" if passed else "ERROR",
                f"Preflight: {name} - {detail}",
            )

    add(
        "ffprobe",
        _executable(config.ffprobe_bin),
        str(config.ffprobe_bin),
    )
    add(
        "MINIMAX_API_KEY",
        bool(config.minimax_api_key),
        "configured" if config.minimax_api_key else "missing",
    )
    add("MINIMAX_MODEL", config.minimax_model == "MiniMax-H3", config.minimax_model)
    add(
        "MINIMAX_RESOLUTION",
        config.minimax_resolution in {"768P", "2K"},
        config.minimax_resolution,
    )
    add(
        "UGUU_UPLOAD_URL",
        config.uguu_upload_url.startswith("https://"),
        config.uguu_upload_url,
    )
    add("target locale", bool(spec.target_locale), spec.target_locale)

    for index, source in enumerate(spec.input_videos, start=1):
        source = Path(source)
        add(
            f"input:{index}:{source.name}",
            source.is_file(),
            "available" if source.is_file() else "file does not exist",
        )

    work_dir = Path(job_dir or config.work_dir)
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        probe_file = work_dir / ".preflight-write-test"
        probe_file.write_text("ok", encoding="utf-8")
        probe_file.unlink(missing_ok=True)
        add("work directory", True, str(work_dir))
    except OSError as exc:
        add("work directory", False, str(exc))

    if execute_remote_checks:
        try:
            uploader.check_access(raw_dir=work_dir / "json" / "raw")
            add("Uguu upload endpoint", True, "endpoint reachable")
        except Exception as exc:  # noqa: BLE001 - surfaced as a check
            add("Uguu upload endpoint", False, str(exc))
        try:
            minimax.check_access(raw_dir=work_dir / "json" / "raw")
            add("MiniMax endpoint configuration", True, "configuration check passed")
        except Exception as exc:  # noqa: BLE001 - surfaced as a check
            add("MiniMax endpoint configuration", False, str(exc))

    return PreflightReport(
        passed=all(check.passed for check in checks if check.fatal),
        checks=checks,
    )


def require_preflight(report: PreflightReport) -> None:
    if report.passed:
        return
    raise PreflightError(
        "Preflight failed; MiniMax workflow was not started",
        checks=[check.model_dump(mode="json") for check in report.checks],
    )
