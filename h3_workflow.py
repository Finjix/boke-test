"""Command-line entry point for the MiniMax H3 segmented workflow."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from config import AppConfig
from core.models import JobSpec
from core.pipeline import VideoLocalizationPipeline
from utils.errors import VideoLocalizerError
from utils.history import HistoryStore
from utils.settings_store import SettingsStore


def _config(project_root: Path) -> AppConfig:
    config = AppConfig.from_env(base_dir=project_root)
    stored = SettingsStore(project_root=project_root).load()
    if not config.minimax_api_key and stored.get("minimax_api_key"):
        config = config.with_overrides(minimax_api_key=stored["minimax_api_key"])
    return config


def _result_payload(result):
    return result.model_dump(mode="json")


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _start(args: argparse.Namespace, project_root: Path) -> None:
    config = _config(project_root)
    pipeline = VideoLocalizationPipeline(config)
    spec = JobSpec(
        input_video=Path(args.video).expanduser(),
        target_language=args.language,
        target_region=args.region,
        target_locale=args.locale,
        reference_images=[Path(item).expanduser() for item in args.reference_image],
        transformation_instruction=args.instruction,
    )
    result = pipeline.run(spec, execution_mode="auto", skip_preflight=args.skip_preflight)
    _print(_result_payload(result))


def _operation(args: argparse.Namespace, project_root: Path) -> None:
    pipeline = VideoLocalizationPipeline(_config(project_root))
    if args.command == "append-segment":
        result = pipeline.append_segment(args.job_id, Path(args.video).expanduser())
    elif args.command == "continue":
        result = pipeline.continue_segment(args.job_id, args.segment)
    elif args.command == "retry":
        result = pipeline.retry_segment(args.job_id, args.segment)
    elif args.command == "finish":
        result = pipeline.finalize(args.job_id)
    else:  # pragma: no cover - argparse choices make this unreachable
        raise ValueError(f"unknown command: {args.command}")
    _print(_result_payload(result))


def _history(project_root: Path) -> None:
    entries = HistoryStore(_config(project_root).work_dir).list_entries()
    _print([entry.model_dump(mode="json") for entry in entries])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MiniMax H3 视频转化与长视频顺序切片工作流")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="开始一个新任务")
    start.add_argument("--video", required=True, help="源视频路径")
    start.add_argument("--language", default="ar", help="目标对白语言代码")
    start.add_argument("--region", default="Saudi Arabia", help="目标地区")
    start.add_argument("--locale", default="ar-SA", help="目标 BCP-47 locale")
    start.add_argument("--reference-image", action="append", default=[], help="参考图，可重复，最多 9 张")
    start.add_argument("--instruction", default="", help="额外转化要求")
    start.add_argument("--skip-preflight", action="store_true", help="仅测试时跳过本地预检")

    append = subparsers.add_parser("append-segment", help="上传并处理下一片 4–15 秒视频")
    append.add_argument("--job-id", required=True)
    append.add_argument("--video", required=True)

    for name, help_text in (
        ("continue", "继续轮询已有 H3 task，不创建重复任务"),
        ("retry", "为失败片段创建新的 H3 task"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--job-id", required=True)
        command.add_argument("--segment", type=int)

    finish = subparsers.add_parser("finish", help="拼接全部已完成片段")
    finish.add_argument("--job-id", required=True)
    subparsers.add_parser("history", help="读取本地执行历史，不调用网络")
    return parser


def main(argv: list[str] | None = None) -> int:
    project_root = Path(__file__).resolve().parent
    args = build_parser().parse_args(argv)
    try:
        if args.command == "start":
            _start(args, project_root)
        elif args.command == "history":
            _history(project_root)
        else:
            _operation(args, project_root)
    except (VideoLocalizerError, OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
