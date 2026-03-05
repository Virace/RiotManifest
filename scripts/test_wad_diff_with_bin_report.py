#!/usr/bin/env python3
"""生成包含 BIN 路径回填的完整 WAD diff 报告."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from riotmanifest.diff import (
    ManifestBinPathProvider,
    ManifestDiffEntry,
    ManifestDiffReport,
    WADHeaderDiffReport,
    diff_manifests,
    diff_wad_headers,
    resolve_wad_diff_paths,
)

DEFAULT_OLD_MANIFEST_URL = (
    "https://lol.secure.dyn.riotcdn.net/channels/public/releases/9FE07DA11C89FD5E.manifest"
)
DEFAULT_NEW_MANIFEST_URL = (
    "https://lol.secure.dyn.riotcdn.net/channels/public/releases/BA80B75282F55531.manifest"
)
DEFAULT_PATTERN = r"\.wad\.client$"
DEFAULT_FLAGS = "zh_CN"
DEFAULT_OUTPUT_REPORT_PATH = (
    Path("out") / "manifest_diff_16_3_to_16_4_with_wad_sections.json"
)
DEFAULT_OUTPUT_WAD_REPORT_PATH = (
    Path("out") / "wad_diff_16_3_to_16_4_with_bin_report.json"
)
DEFAULT_OUTPUT_TIMING_PATH = (
    Path("out") / "wad_diff_16_3_to_16_4_with_bin_report_timing.json"
)
DEFAULT_BIN_DATA_SOURCE_MODE = "extractor"
TARGET_SECTION_STATUSES = {"added", "removed", "changed"}


@dataclass(frozen=True)
class DiffRunResult:
    """保存一次完整执行的关键产物与耗时."""

    resolved_report: WADHeaderDiffReport
    target_wad_files: tuple[str, ...]
    timing_seconds: dict[str, float]


def _build_argument_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器."""
    parser = argparse.ArgumentParser(
        description="执行 Manifest/WAD diff，并导出 BIN 回填后的完整 WAD 报告。",
    )
    parser.add_argument(
        "--old-manifest",
        default=DEFAULT_OLD_MANIFEST_URL,
        help="旧版本 manifest 路径或 URL。",
    )
    parser.add_argument(
        "--new-manifest",
        default=DEFAULT_NEW_MANIFEST_URL,
        help="新版本 manifest 路径或 URL。",
    )
    parser.add_argument(
        "--flags",
        default=DEFAULT_FLAGS,
        help="按逗号分隔的 flag 过滤（例如: zh_CN,en_US）；留空表示不过滤。",
    )
    parser.add_argument(
        "--pattern",
        default=DEFAULT_PATTERN,
        help="目标文件正则（默认仅处理 .wad.client）。",
    )
    parser.add_argument(
        "--target-wad",
        action="append",
        default=[],
        help="显式指定目标 WAD，可重复传参；不传则自动使用 manifest diff 的增删改结果。",
    )
    parser.add_argument(
        "--include-unchanged",
        action="store_true",
        help="是否在 WAD 头部对比中包含 unchanged 条目。",
    )
    parser.add_argument(
        "--max-skin-id",
        type=int,
        default=100,
        help="英雄 skinN.bin 的最大 N（默认 100）。",
    )
    parser.add_argument(
        "--bin-data-source-mode",
        choices=("extractor", "download_root_wad"),
        default=DEFAULT_BIN_DATA_SOURCE_MODE,
        help="BIN 数据来源模式：extractor 或 download_root_wad。",
    )
    parser.add_argument(
        "--root-wad-download-dir",
        type=Path,
        default=None,
        help="download_root_wad 模式下 root WAD 下载目录。",
    )
    parser.add_argument(
        "--keep-downloaded-root-wads",
        action="store_true",
        help="download_root_wad 模式下是否保留下载的 root WAD 文件。",
    )
    parser.add_argument(
        "--download-map-root-wads",
        action="store_true",
        help="download_root_wad 模式下是否对地图类 WAD 也走整包下载。",
    )
    parser.add_argument(
        "--root-wad-download-concurrency-limit",
        type=int,
        default=None,
        help="download_root_wad 模式下 root WAD 下载并发上限。",
    )
    parser.add_argument(
        "--hash-type-mismatch-mode",
        choices=("loose", "strict"),
        default="loose",
        help="chunk_hash_types 不一致时处理策略。",
    )
    parser.add_argument(
        "--output-report",
        type=Path,
        default=DEFAULT_OUTPUT_REPORT_PATH,
        help="最终清爽报告输出路径（ManifestDiffEntry + section_diffs）。",
    )
    parser.add_argument(
        "--output-wad-report",
        type=Path,
        default=DEFAULT_OUTPUT_WAD_REPORT_PATH,
        help="完整 WADHeaderDiffReport 输出路径（调试用）。",
    )
    parser.add_argument(
        "--output-timing",
        type=Path,
        default=DEFAULT_OUTPUT_TIMING_PATH,
        help="执行耗时与回填统计输出路径。",
    )
    parser.add_argument(
        "--collapse-manifest-equal-pairs",
        action="store_true",
        help="导出报告时压缩 manifest_report 中 old/new 相同字段。",
    )
    return parser


def _parse_flags(raw_flags: str) -> str | tuple[str, ...] | None:
    """解析命令行 flags 字符串为 diff API 可接受的类型."""
    cleaned = raw_flags.strip()
    if not cleaned:
        return None
    parts = tuple(item.strip() for item in cleaned.split(",") if item.strip())
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return parts


def _normalize_target_paths(paths: Iterable[str]) -> tuple[str, ...]:
    """对路径做去重与大小写无关归一化."""
    deduplicated: dict[str, str] = {}
    for path in paths:
        cleaned = path.strip().replace("\\", "/")
        if not cleaned:
            continue
        deduplicated.setdefault(cleaned.lower(), cleaned)
    return tuple(deduplicated[key] for key in sorted(deduplicated))


def _collect_target_wads(
    manifest_report: ManifestDiffReport,
    explicit_targets: Iterable[str],
) -> tuple[str, ...]:
    """收集本次 WAD 头对比目标路径."""
    normalized_explicit_targets = _normalize_target_paths(explicit_targets)
    if normalized_explicit_targets:
        return normalized_explicit_targets

    auto_targets = _normalize_target_paths(
        _iter_paths(
            entries=(
                *manifest_report.changed,
                *manifest_report.added,
                *manifest_report.removed,
            ),
        )
    )
    if not auto_targets:
        raise ValueError("未从 manifest diff 中筛到任何 WAD，请检查 flags/pattern。")
    return auto_targets


def _iter_paths(entries: Iterable[ManifestDiffEntry]) -> Iterable[str]:
    """遍历条目并仅返回 WAD 路径."""
    for entry in entries:
        lowered = entry.path.lower()
        if not lowered.endswith(".wad.client"):
            continue
        yield entry.path


def _execute_full_diff(
    *,
    old_manifest: str,
    new_manifest: str,
    flags: str | tuple[str, ...] | None,
    pattern: str,
    explicit_targets: Iterable[str],
    include_unchanged: bool,
    max_skin_id: int,
    bin_data_source_mode: str,
    root_wad_download_dir: Path | None,
    keep_downloaded_root_wads: bool,
    download_map_root_wads: bool,
    root_wad_download_concurrency_limit: int | None,
    hash_type_mismatch_mode: str,
) -> DiffRunResult:
    """执行完整 diff 与 BIN 回填流程."""
    total_start = perf_counter()

    stage_start = perf_counter()
    manifest_report = diff_manifests(
        old_manifest,
        new_manifest,
        flags=flags,
        pattern=pattern,
        include_unchanged=False,
        detect_moves=False,
        hash_type_mismatch_mode=hash_type_mismatch_mode,
    )
    manifest_elapsed = perf_counter() - stage_start

    target_wad_files = _collect_target_wads(manifest_report, explicit_targets)

    stage_start = perf_counter()
    wad_report = diff_wad_headers(
        manifest_report=manifest_report,
        target_wad_files=target_wad_files,
        include_unchanged=include_unchanged,
        hash_type_mismatch_mode=hash_type_mismatch_mode,
    )
    wad_elapsed = perf_counter() - stage_start

    stage_start = perf_counter()
    with ManifestBinPathProvider(max_skin_id=max_skin_id) as provider:
        resolved_report = resolve_wad_diff_paths(
            wad_report,
            path_provider=provider,
            bin_data_source_mode=bin_data_source_mode,
            root_wad_download_dir=root_wad_download_dir,
            cleanup_downloaded_root_wads=not keep_downloaded_root_wads,
            download_map_root_wads=download_map_root_wads,
            root_wad_download_concurrency_limit=root_wad_download_concurrency_limit,
        )
    resolve_elapsed = perf_counter() - stage_start

    total_elapsed = perf_counter() - total_start

    timing_seconds = {
        "diff_manifests": round(manifest_elapsed, 3),
        "diff_wad_headers": round(wad_elapsed, 3),
        "resolve_wad_diff_paths_with_bin": round(resolve_elapsed, 3),
        "total": round(total_elapsed, 3),
    }
    return DiffRunResult(
        resolved_report=resolved_report,
        target_wad_files=target_wad_files,
        timing_seconds=timing_seconds,
    )


def _build_section_stats(report: WADHeaderDiffReport) -> dict[str, int | float | dict[str, int]]:
    """统计 section 回填命中率."""
    status_stats = {"added": 0, "removed": 0, "changed": 0, "unchanged": 0}
    resolved_with_path = 0
    unresolved = 0

    for file_entry in report.files:
        for section in file_entry.section_diffs:
            status_stats[section.status] = status_stats.get(section.status, 0) + 1
            if section.status not in TARGET_SECTION_STATUSES:
                continue
            if section.path is None:
                unresolved += 1
                continue
            resolved_with_path += 1

    target_statuses_total = resolved_with_path + unresolved
    resolve_ratio = (
        0.0 if target_statuses_total == 0 else round(resolved_with_path / target_statuses_total, 6)
    )
    return {
        "all_status_stats": status_stats,
        "target_statuses_total": target_statuses_total,
        "resolved_with_path": resolved_with_path,
        "unresolved": unresolved,
        "resolve_ratio": resolve_ratio,
    }


def _write_json(path: Path, payload: dict[str, object]) -> str:
    """将 JSON 数据写入文件并返回标准化路径."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def main() -> None:
    """脚本入口."""
    parser = _build_argument_parser()
    args = parser.parse_args()

    result = _execute_full_diff(
        old_manifest=args.old_manifest,
        new_manifest=args.new_manifest,
        flags=_parse_flags(args.flags),
        pattern=args.pattern,
        explicit_targets=args.target_wad,
        include_unchanged=args.include_unchanged,
        max_skin_id=args.max_skin_id,
        bin_data_source_mode=args.bin_data_source_mode,
        root_wad_download_dir=args.root_wad_download_dir,
        keep_downloaded_root_wads=args.keep_downloaded_root_wads,
        download_map_root_wads=args.download_map_root_wads,
        root_wad_download_concurrency_limit=args.root_wad_download_concurrency_limit,
        hash_type_mismatch_mode=args.hash_type_mismatch_mode,
    )

    manifest_with_sections_output = result.resolved_report.manifest_report.dump_pretty_json(
        args.output_report,
        collapse_equal_pairs=args.collapse_manifest_equal_pairs,
    )
    resolved_report_output = result.resolved_report.dump_pretty_json(
        args.output_wad_report,
        collapse_manifest_equal_pairs=args.collapse_manifest_equal_pairs,
    )
    timing_payload = {
        "target_wad_count": len(result.target_wad_files),
        "target_wad_files": result.target_wad_files,
        "timing_seconds": result.timing_seconds,
        "sections": _build_section_stats(result.resolved_report),
        "bin_data_source_mode": args.bin_data_source_mode,
        "root_wad_download_dir": str(args.root_wad_download_dir) if args.root_wad_download_dir else None,
        "keep_downloaded_root_wads": args.keep_downloaded_root_wads,
        "download_map_root_wads": args.download_map_root_wads,
        "root_wad_download_concurrency_limit": args.root_wad_download_concurrency_limit,
        "manifest_with_sections_report_path": manifest_with_sections_output,
        "resolved_report_path": resolved_report_output,
    }
    timing_output = _write_json(args.output_timing, timing_payload)

    print(json.dumps(timing_payload, ensure_ascii=False, indent=2))
    print(f"manifest report saved: {manifest_with_sections_output}")
    print(f"resolved wad report saved: {resolved_report_output}")
    print(f"timing summary saved: {timing_output}")


if __name__ == "__main__":
    main()
