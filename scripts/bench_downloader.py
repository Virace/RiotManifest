#!/usr/bin/env python3
"""Downloader 多轮基准测试脚本.

用于在同一批目标文件上重复执行下载，输出每轮吞吐与中位数汇总，
便于和其他实现（例如 Go 版本）做稳定对比。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from statistics import median
from time import perf_counter

from riotmanifest.downloader import DownloadProgress
from riotmanifest.manifest import PatcherFile, PatcherManifest

DEFAULT_FLAG = "ja_JP"
DEFAULT_PATTERN = "wad.client"
DEFAULT_CONCURRENCY = 16
DEFAULT_ROUNDS = 3
DEFAULT_PROGRESS_INTERVAL_SECONDS = 0.5
DEFAULT_OUTPUT_ROOT = Path("out") / "downloader_bench_runs"
DEFAULT_OUTPUT_JSON = Path("out") / "downloader_bench_summary.json"
MIB = 1024 * 1024
GIB = 1024 * 1024 * 1024
MILESTONE_RATIOS = (0.1, 0.25, 0.5, 0.75, 0.9, 1.0)


@dataclass(frozen=True)
class MilestoneResult:
    """里程碑进度点的速度快照."""

    percent: int
    elapsed_seconds: float
    average_speed_mb_per_sec: float
    segment_speed_mb_per_sec: float
    finished_jobs: int
    total_jobs: int


@dataclass(frozen=True)
class RoundBenchResult:
    """单轮下载基准结果."""

    round_index: int
    output_dir: str
    elapsed_seconds: float
    downloaded_bytes: int
    downloaded_gib: float
    throughput_mb_per_sec: float
    milestones: tuple[MilestoneResult, ...]


def _build_argument_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器."""
    parser = argparse.ArgumentParser(
        description="执行 downloader 多轮基准测试并输出中位数统计。",
    )
    parser.add_argument(
        "manifest",
        help="manifest 本地路径或 URL。",
    )
    parser.add_argument(
        "--flag",
        default=DEFAULT_FLAG,
        help="文件 flag 过滤（默认 ja_JP）。",
    )
    parser.add_argument(
        "--pattern",
        default=DEFAULT_PATTERN,
        help="文件名正则过滤（默认 wad.client）。",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help="下载并发 worker 数（默认 16）。",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=DEFAULT_ROUNDS,
        help="基准轮数（默认 3）。",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=0,
        help="最多下载文件数（0 表示不限制）。",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="每轮下载输出目录根路径。",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=DEFAULT_OUTPUT_JSON,
        help="基准汇总 JSON 输出路径。",
    )
    parser.add_argument(
        "--progress-interval-seconds",
        type=float,
        default=DEFAULT_PROGRESS_INTERVAL_SECONDS,
        help="进度 tick 上报间隔秒数（<=0 表示禁用周期上报）。",
    )
    parser.add_argument(
        "--keep-output",
        action="store_true",
        help="是否保留每轮下载输出目录。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅输出计划，不执行下载。",
    )
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    """校验参数合法性."""
    if args.concurrency <= 0:
        raise ValueError("--concurrency 必须大于 0。")
    if args.rounds <= 0:
        raise ValueError("--rounds 必须大于 0。")
    if args.max_files < 0:
        raise ValueError("--max-files 不能小于 0。")


def _select_target_files(
    manifest: PatcherManifest,
    *,
    flag: str | None,
    pattern: str | None,
    max_files: int,
) -> tuple[PatcherFile, ...]:
    """按条件筛选并返回稳定顺序的目标文件列表."""
    selected = [
        file
        for file in manifest.filter_files(flag=flag, pattern=pattern)
        if not file.link and file.size > 0
    ]
    selected.sort(key=lambda item: item.name.lower())
    if max_files > 0:
        selected = selected[:max_files]
    if not selected:
        raise ValueError(
            f"未筛到可下载文件：flag={flag!r}, pattern={pattern!r}, max_files={max_files}"
        )
    return tuple(selected)


def _verify_and_sum_downloaded_bytes(
    manifest: PatcherManifest,
    files: tuple[PatcherFile, ...],
) -> int:
    """校验每个输出文件大小并汇总下载字节."""
    downloaded_bytes = 0
    for file in files:
        output = Path(manifest.file_output(file))
        if not output.is_file():
            raise RuntimeError(f"下载输出缺失: {file.name}")
        file_size = output.stat().st_size
        if file_size != file.size:
            raise RuntimeError(
                f"下载文件大小不匹配: {file.name}, actual={file_size}, expected={file.size}"
            )
        downloaded_bytes += file_size
    return downloaded_bytes


def _build_milestones(samples: list[DownloadProgress]) -> tuple[MilestoneResult, ...]:
    """把进度采样转换为固定里程碑结果."""
    if not samples:
        return tuple()

    compact_samples: list[DownloadProgress] = []
    for sample in samples:
        if not compact_samples:
            compact_samples.append(sample)
            continue
        if sample.finished_bytes == compact_samples[-1].finished_bytes:
            compact_samples[-1] = sample
            continue
        compact_samples.append(sample)

    total_bytes = compact_samples[-1].total_bytes
    if total_bytes <= 0:
        return tuple()

    output: list[MilestoneResult] = []
    previous_elapsed = 0.0
    previous_finished_bytes = 0
    for ratio in MILESTONE_RATIOS:
        target_bytes = int(total_bytes * ratio)
        hit = next((sample for sample in compact_samples if sample.finished_bytes >= target_bytes), None)
        if hit is None:
            continue
        delta_elapsed = max(hit.elapsed_seconds - previous_elapsed, 1e-9)
        delta_bytes = max(hit.finished_bytes - previous_finished_bytes, 0)
        output.append(
            MilestoneResult(
                percent=int(ratio * 100),
                elapsed_seconds=round(hit.elapsed_seconds, 3),
                average_speed_mb_per_sec=round(hit.average_speed_bytes_per_sec / MIB, 2),
                segment_speed_mb_per_sec=round(delta_bytes / MIB / delta_elapsed, 2),
                finished_jobs=hit.finished_jobs,
                total_jobs=hit.total_jobs,
            )
        )
        previous_elapsed = hit.elapsed_seconds
        previous_finished_bytes = hit.finished_bytes

    return tuple(output)


def _run_single_round(
    *,
    manifest: PatcherManifest,
    files: tuple[PatcherFile, ...],
    round_index: int,
    output_root: Path,
    concurrency: int,
    progress_interval_seconds: float | None,
    keep_output: bool,
) -> RoundBenchResult:
    """执行单轮下载并返回基准结果."""
    output_dir = output_root / f"round_{round_index:02d}"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest.path = str(output_dir)

    samples: list[DownloadProgress] = []

    def on_progress(progress: DownloadProgress) -> None:
        if progress.phase in {"start", "tick", "bundle_completed", "completed"}:
            samples.append(progress)

    start = perf_counter()
    results = asyncio.run(
        manifest.download_files_concurrently(
            list(files),
            concurrency_limit=concurrency,
            raise_on_error=True,
            progress_callback=on_progress,
            progress_interval_seconds=progress_interval_seconds,
        )
    )
    elapsed_seconds = perf_counter() - start
    if not all(results):
        raise RuntimeError(f"第 {round_index} 轮存在下载失败项。")

    downloaded_bytes = _verify_and_sum_downloaded_bytes(manifest, files)
    throughput = downloaded_bytes / MIB / max(elapsed_seconds, 1e-9)
    milestones = _build_milestones(samples)

    round_result = RoundBenchResult(
        round_index=round_index,
        output_dir=str(output_dir),
        elapsed_seconds=round(elapsed_seconds, 3),
        downloaded_bytes=downloaded_bytes,
        downloaded_gib=round(downloaded_bytes / GIB, 3),
        throughput_mb_per_sec=round(throughput, 2),
        milestones=milestones,
    )

    if not keep_output:
        shutil.rmtree(output_dir, ignore_errors=True)
    return round_result


def _build_summary_payload(
    *,
    args: argparse.Namespace,
    target_files: tuple[PatcherFile, ...],
    planned_bytes: int,
    job_count: int,
    bundle_count: int,
    range_count: int,
    unique_chunk_count: int,
    rounds: tuple[RoundBenchResult, ...],
) -> dict[str, object]:
    """构建可序列化的汇总 JSON 对象."""
    throughput_values = [result.throughput_mb_per_sec for result in rounds]
    elapsed_values = [result.elapsed_seconds for result in rounds]

    aggregate = {
        "median_throughput_mb_per_sec": round(float(median(throughput_values)), 2),
        "best_throughput_mb_per_sec": round(max(throughput_values), 2),
        "worst_throughput_mb_per_sec": round(min(throughput_values), 2),
        "median_elapsed_seconds": round(float(median(elapsed_values)), 3),
    }

    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "config": {
            "manifest": args.manifest,
            "flag": args.flag,
            "pattern": args.pattern,
            "concurrency": args.concurrency,
            "rounds": args.rounds,
            "max_files": args.max_files,
            "output_root": str(args.output_root),
            "keep_output": bool(args.keep_output),
            "progress_interval_seconds": (
                args.progress_interval_seconds if args.progress_interval_seconds > 0 else None
            ),
        },
        "plan": {
            "target_file_count": len(target_files),
            "planned_bytes": planned_bytes,
            "planned_gib": round(planned_bytes / GIB, 3),
            "job_count": job_count,
            "bundle_count": bundle_count,
            "range_count": range_count,
            "unique_chunk_count": unique_chunk_count,
        },
        "aggregate": aggregate,
        "rounds": [asdict(item) for item in rounds],
    }


def _print_plan(
    *,
    manifest: str,
    flag: str,
    pattern: str,
    concurrency: int,
    rounds: int,
    target_file_count: int,
    planned_bytes: int,
    job_count: int,
    bundle_count: int,
    range_count: int,
    unique_chunk_count: int,
) -> None:
    """输出测试计划摘要."""
    print("[BENCH] 下载计划")
    print(f"[BENCH] manifest={manifest}")
    print(f"[BENCH] filter: flag={flag}, pattern={pattern}")
    print(f"[BENCH] rounds={rounds}, concurrency={concurrency}")
    print(
        "[BENCH] "
        f"files={target_file_count}, planned={planned_bytes / GIB:.3f}GiB, "
        f"jobs={job_count}, bundles={bundle_count}, ranges={range_count}, "
        f"unique_chunks={unique_chunk_count}"
    )


def _print_round_result(result: RoundBenchResult) -> None:
    """输出单轮结果摘要."""
    print(
        "[BENCH] "
        f"round={result.round_index} elapsed={result.elapsed_seconds:.3f}s "
        f"throughput={result.throughput_mb_per_sec:.2f}MB/s "
        f"downloaded={result.downloaded_gib:.3f}GiB"
    )
    for milestone in result.milestones:
        print(
            "[BENCH] "
            f"round={result.round_index} m{milestone.percent:>3}% "
            f"t={milestone.elapsed_seconds:.3f}s "
            f"avg={milestone.average_speed_mb_per_sec:.2f}MB/s "
            f"seg={milestone.segment_speed_mb_per_sec:.2f}MB/s "
            f"jobs={milestone.finished_jobs}/{milestone.total_jobs}"
        )


def _write_json(path: Path, payload: dict[str, object]) -> None:
    """写入 JSON 文件."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    """脚本入口."""
    parser = _build_argument_parser()
    args = parser.parse_args()
    _validate_args(args)

    probe_dir = args.output_root / "_probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    manifest = PatcherManifest(
        file=args.manifest,
        path=str(probe_dir),
        concurrency_limit=args.concurrency,
    )
    target_files = _select_target_files(
        manifest,
        flag=args.flag,
        pattern=args.pattern,
        max_files=args.max_files,
    )
    planned_bytes = sum(file.size for file in target_files)
    jobs = manifest.downloader.build_bundle_jobs(list(target_files))
    bundle_count = len({job.bundle_id for job in jobs})
    range_count = sum(len(job.ranges) for job in jobs)
    unique_chunk_count = sum(
        len(tasks) for tasks in manifest.downloader.build_global_task_map(list(target_files)).values()
    )
    _print_plan(
        manifest=args.manifest,
        flag=args.flag,
        pattern=args.pattern,
        concurrency=args.concurrency,
        rounds=args.rounds,
        target_file_count=len(target_files),
        planned_bytes=planned_bytes,
        job_count=len(jobs),
        bundle_count=bundle_count,
        range_count=range_count,
        unique_chunk_count=unique_chunk_count,
    )

    if args.dry_run:
        print("[BENCH] dry-run 模式，不执行下载。")
        shutil.rmtree(probe_dir, ignore_errors=True)
        return

    rounds: list[RoundBenchResult] = []
    progress_interval = args.progress_interval_seconds if args.progress_interval_seconds > 0 else None
    try:
        for round_index in range(1, args.rounds + 1):
            round_result = _run_single_round(
                manifest=manifest,
                files=target_files,
                round_index=round_index,
                output_root=args.output_root,
                concurrency=args.concurrency,
                progress_interval_seconds=progress_interval,
                keep_output=args.keep_output,
            )
            rounds.append(round_result)
            _print_round_result(round_result)
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)

    summary = _build_summary_payload(
        args=args,
        target_files=target_files,
        planned_bytes=planned_bytes,
        job_count=len(jobs),
        bundle_count=bundle_count,
        range_count=range_count,
        unique_chunk_count=unique_chunk_count,
        rounds=tuple(rounds),
    )
    _write_json(args.output_json, summary)

    aggregate = summary["aggregate"]
    print(
        "[BENCH] "
        f"median={aggregate['median_throughput_mb_per_sec']:.2f}MB/s "
        f"best={aggregate['best_throughput_mb_per_sec']:.2f}MB/s "
        f"worst={aggregate['worst_throughput_mb_per_sec']:.2f}MB/s"
    )
    print(f"[BENCH] summary={args.output_json}")


if __name__ == "__main__":
    main()
