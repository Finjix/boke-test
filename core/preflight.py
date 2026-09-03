"""Startup checks that must pass before the formal pipeline can run."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from api.minimax import MiniMaxClient
from api.seedance import SeedanceClient
from api.uguu import UguuClient
from config import (
    AppConfig,
    DOUBAO_BASE64_MAX_REQUEST_MIB,
    DOUBAO_BASE64_MAX_VIDEO_MIB,
    DOUBAO_VIDEO_INPUT_MODES,
    FIXED_DOUBAO_MODEL,
    FIXED_SEEDREAM_MODEL,
)
from core.models import JobSpec, PreflightCheck, PreflightReport
from language_config import is_h3_native_language
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
    active_h3_requested = (
        "minimax_h3" in clients
        or "h3" in clients
        or ("ark" in clients and "seedance" not in clients)
        or (not clients.get("seedance") and bool(config.minimax_api_key))
    )
    if active_h3_requested:
        return run_h3_preflight(
            config,
            spec,
            job_dir=job_dir,
            ark_client=clients.get("ark"),
            minimax_client=clients.get("minimax_h3") or clients.get("h3"),
            uguu_client=clients.get("uguu"),
            logger=logger,
            execute_remote_checks=execute_remote_checks,
        )
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


def run_h3_preflight(
    config: AppConfig,
    spec: JobSpec,
    *,
    job_dir: Path | None = None,
    ark_client: Any | None = None,
    minimax_client: Any | None = None,
    uguu_client: Any | None = None,
    logger: JobLogger | None = None,
    execute_remote_checks: bool = True,
) -> PreflightReport:
    """Run non-generating checks for the active MiniMax H3 workflow."""

    checks: list[PreflightCheck] = []

    def add(name: str, passed: bool, detail: str, *, fatal: bool = True) -> None:
        checks.append(PreflightCheck(name=name, passed=passed, detail=detail, fatal=fatal))
        if logger:
            logger.emit("INFO" if passed else "ERROR", f"Preflight: {name} - {detail}")

    add(
        "ffprobe",
        _executable(config.ffprobe_bin),
        str(config.ffprobe_bin),
    )
    add(
        "ARK_API_KEY",
        bool(config.ark_api_key),
        "configured" if config.ark_api_key else "missing",
    )
    add(
        "DOUBAO_MODEL",
        config.doubao_model == FIXED_DOUBAO_MODEL,
        config.doubao_model,
    )
    add(
        "DOUBAO_VIDEO_INPUT_MODE",
        config.doubao_video_input_mode.casefold() in DOUBAO_VIDEO_INPUT_MODES,
        config.doubao_video_input_mode,
    )
    add(
        "SEEDREAM_MODEL",
        config.seedream_model == FIXED_SEEDREAM_MODEL,
        config.seedream_model,
    )
    add(
        "ARK_BASE_URL",
        config.ark_base_url.startswith(("http://", "https://")),
        config.ark_base_url,
    )
    add(
        "MINIMAX_API_KEY",
        bool(config.minimax_api_key),
        "configured" if config.minimax_api_key else "missing",
    )
    add(
        "MINIMAX_MODEL",
        config.minimax_model == "MiniMax-H3",
        config.minimax_model,
    )
    add(
        "UGUU_UPLOAD_URL",
        config.uguu_upload_url.startswith("https://"),
        config.uguu_upload_url,
    )
    add(
        "H3 target language",
        is_h3_native_language(spec.target_language),
        spec.target_language,
    )

    # v7 never accepts user-supplied reference images.  Doubao selects the
    # storyboard keyframes and Seedream creates the target references after
    # the first approval, so the only input media to preflight is the source
    # master video.
    for path in [spec.input_video]:
        path = Path(path)
        exists = path.is_file()
        add(f"input:{path.name}", exists, "available" if exists else "file does not exist")
        if exists:
            limit = config.uguu_max_file_mib * 1024 * 1024
            within = path.stat().st_size <= limit
            add(
                f"size:{path.name}",
                within,
                "within Uguu input limit" if within else f"larger than {limit // (1024 * 1024)} MiB",
            )
            if config.doubao_video_input_mode.casefold() == "base64":
                byte_size = path.stat().st_size
                estimated_request_bytes = ((byte_size + 2) // 3) * 4 + 256 * 1024
                base64_within = (
                    byte_size <= DOUBAO_BASE64_MAX_VIDEO_MIB * 1024 * 1024
                    and estimated_request_bytes <= DOUBAO_BASE64_MAX_REQUEST_MIB * 1024 * 1024
                )
                add(
                    f"doubao-base64-size:{path.name}",
                    base64_within,
                    (
                        f"within {DOUBAO_BASE64_MAX_VIDEO_MIB} MiB video and "
                        f"{DOUBAO_BASE64_MAX_REQUEST_MIB} MiB request limits"
                        if base64_within
                        else "too large for Doubao Base64 input"
                    ),
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
        uploader = uguu_client or UguuClient(config, logger=logger)
        try:
            uploader.check_access(raw_dir=work_dir / "json" / "raw")
            add("Uguu upload endpoint (H3/Seedream)", True, "endpoint reachable")
        except Exception as exc:  # noqa: BLE001 - surfaced as a check
            add("Uguu upload endpoint (H3/Seedream)", False, str(exc))
        h3 = minimax_client or MiniMaxClient(config, logger=logger)
        try:
            h3.check_access(raw_dir=work_dir / "json" / "raw")
            add("MiniMax H3 configuration", True, "configuration check passed")
        except Exception as exc:  # noqa: BLE001 - surfaced as a check
            add("MiniMax H3 configuration", False, str(exc))
        # Ark has no non-generating probe in this application.  The fixed
        # models, endpoint and credential checks above gate the first paid
        # analysis/image calls without issuing a throwaway completion request.
        add(
            "Doubao / Seedream endpoint",
            ark_client is not None or bool(config.ark_api_key),
            "deferred to the persisted Doubao or Seedream call"
            if ark_client is not None or config.ark_api_key
            else "Ark client or API key is not configured",
        )

    return PreflightReport(
        passed=all(check.passed for check in checks if check.fatal),
        checks=checks,
    )


def require_preflight(report: PreflightReport) -> None:
    if report.passed:
        return
    raise PreflightError(
        "Preflight failed; formal Pipeline was not started",
        checks=[check.model_dump(mode="json") for check in report.checks],
    )
