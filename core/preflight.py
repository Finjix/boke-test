"""Startup checks that must pass before the formal pipeline can run."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from api.seedance import SeedanceClient
from api.uguu import UguuClient
from config import AppConfig, FIXED_DOUBAO_MODEL
from core.models import JobSpec, PreflightCheck, PreflightReport
from utils.errors import PreflightError
from utils.logger import JobLogger


def _executable(value: str) -> bool:
    path = Path(value).expanduser()
    return path.is_file() or shutil.which(value) is not None


class PreflightRunner:
    def __init__(
        self,
        config: AppConfig,
        *,
        seedance_client: Any | None = None,
        uguu_client: Any | None = None,
        logger: JobLogger | None = None,
    ):
        self.config = config
        self.seedance_client = seedance_client or SeedanceClient(config, logger=logger)
        self.uguu_client = uguu_client or UguuClient(config, logger=logger)
        self.logger = logger

    def run(
        self,
        spec: JobSpec,
        *,
        job_dir: Path | None = None,
        execute_remote_checks: bool = True,
    ) -> PreflightReport:
        checks: list[PreflightCheck] = []

        def add(name: str, passed: bool, detail: str, *, fatal: bool = True) -> None:
            check = PreflightCheck(name=name, passed=passed, detail=detail, fatal=fatal)
            checks.append(check)
            if self.logger:
                level = "info" if passed else "error"
                self.logger.emit(level, f"Preflight: {name} - {detail}")

        executable = self.config.ffprobe_bin
        passed = _executable(executable)
        add("ffprobe", passed, executable if passed else f"not found: {executable}")

        for name, value in (
            ("ARK_API_KEY", self.config.ark_api_key),
            ("SEEDANCE_MODEL_ID", self.config.seedance_model_id),
        ):
            add(name, bool(value), "configured" if value else "missing")

        add("DOUBAO_MODEL", self.config.doubao_model == FIXED_DOUBAO_MODEL, self.config.doubao_model)
        add(
            "UGUU_UPLOAD_URL",
            self.config.uguu_upload_url.startswith("https://"),
            self.config.uguu_upload_url,
        )
        add(
            "UGUU_EXPIRE_HOURS",
            self.config.uguu_expire_hours > 0,
            str(self.config.uguu_expire_hours),
        )

        source_paths = [spec.input_video, *spec.character_refs, *spec.scene_refs]
        for path in source_paths:
            exists = Path(path).is_file()
            add(
                f"input:{Path(path).name}",
                exists,
                "available" if exists else "file does not exist",
            )
            if exists:
                max_bytes = self.config.uguu_max_file_mib * 1024 * 1024
                within_limit = Path(path).stat().st_size <= max_bytes
                add(
                    f"size:{Path(path).name}",
                    within_limit,
                    "within configured Uguu limit"
                    if within_limit
                    else f"larger than {self.config.uguu_max_file_mib} MiB",
                )

        work_dir = Path(job_dir or self.config.work_dir)
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
                self.uguu_client.check_access(raw_dir=work_dir / "json" / "raw")
                add("Uguu upload endpoint", True, "endpoint reachable")
            except Exception as exc:  # noqa: BLE001 - surfaced as a check
                add("Uguu upload endpoint", False, str(exc))

            if self.config.ark_api_key and self.config.seedance_model_id:
                try:
                    self.seedance_client.check_access(raw_dir=work_dir / "json" / "raw")
                    add("Seedance endpoint access", True, "endpoint probe succeeded")
                except Exception as exc:  # noqa: BLE001 - surfaced as a check
                    add("Seedance endpoint access", False, str(exc))
            else:
                add("Seedance endpoint access", False, "API key or model ID missing")

        passed = all(check.passed for check in checks if check.fatal)
        return PreflightReport(passed=passed, checks=checks)


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
    runner = PreflightRunner(
        config,
        seedance_client=clients.get("seedance"),
        uguu_client=clients.get("uguu"),
        logger=logger,
    )
    return runner.run(
        spec,
        job_dir=job_dir,
        execute_remote_checks=execute_remote_checks,
    )


def require_preflight(report: PreflightReport) -> None:
    if report.passed:
        return
    raise PreflightError(
        "Preflight failed; formal Pipeline was not started",
        checks=[check.model_dump(mode="json") for check in report.checks],
    )
