"""WAD 头部差异分析能力."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from os import PathLike
from pathlib import Path
from typing import Any, Literal

from riotmanifest.diff.manifest_diff import (
    DEFAULT_OVERLAP_WARNING_THRESHOLD,
    HashTypeMismatchMode,
    ManifestDiffEntry,
    ManifestDiffReport,
    ManifestDiffStatus,
    ManifestInput,
    _collapse_equal_manifest_fields,
    _collect_target_files,
    _diff_file_fields,
    _ensure_manifest,
    _normalize_target_files,
    _validate_overlap_threshold,
    diff_manifests,
)
from riotmanifest.extractor.wad_extractor import WADExtractor
from riotmanifest.manifest import PatcherFile, PatcherManifest

WADDiffStatus = Literal["added", "removed", "changed", "unchanged", "error"]
ManifestReportRenderMode = Literal["full", "summary", "none"]
WAD_FILE_PATTERN = r"\.wad\.client$"


@dataclass(frozen=True)
class WADSectionSignature:
    """WAD 文件内部 section 快照签名."""

    path_hash: int
    size: int
    compressed_size: int
    section_type: int
    subchunk_count: int
    duplicate: bool
    sha256: int | None

    @classmethod
    def from_section(cls, section: Any) -> WADSectionSignature:
        """从 `league_tools` 的 WADSection 对象提取稳定可比较字段.

        Args:
            section: WADSection 对象。

        Returns:
            结构化签名。
        """
        subchunk_count = section.subchunk_count if hasattr(section, "subchunk_count") else 0
        duplicate = section.duplicate if hasattr(section, "duplicate") else False
        sha256 = section.sha256 if hasattr(section, "sha256") else None
        return cls(
            path_hash=int(section.path_hash),
            size=int(section.size),
            compressed_size=int(section.compressed_size),
            section_type=int(section.type),
            subchunk_count=int(subchunk_count),
            duplicate=bool(duplicate),
            sha256=_as_optional_int(sha256),
        )

    def sort_key(self) -> tuple[int, int, int, int, bool, int]:
        """返回可排序键，便于比较 section 多重集合。."""
        return (
            self.size,
            self.compressed_size,
            self.section_type,
            self.subchunk_count,
            self.duplicate,
            self.sha256 if self.sha256 is not None else -1,
        )


@dataclass(frozen=True)
class WADSectionDiffEntry:
    """WAD 内单个 path_hash 的差异条目."""

    path_hash: int
    status: ManifestDiffStatus
    old_sections: tuple[WADSectionSignature, ...]
    new_sections: tuple[WADSectionSignature, ...]
    path: str | None = None


@dataclass(frozen=True)
class WADFileDiffEntry:
    """单个 WAD 文件的头部差异条目."""

    wad_path: str
    status: WADDiffStatus
    section_diffs: tuple[WADSectionDiffEntry, ...]
    missing_focused_paths: tuple[str, ...]
    warning: str | None = None


@dataclass(frozen=True)
class WADHeaderDiffSummary:
    """WAD 头部差异汇总信息."""

    total_wads: int
    changed_count: int
    unchanged_count: int
    added_count: int
    removed_count: int
    error_count: int
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class WADHeaderDiffReport:
    """WAD 头部差异分析结果."""

    summary: WADHeaderDiffSummary
    files: tuple[WADFileDiffEntry, ...]
    manifest_report: ManifestDiffReport

    def to_dict(
        self,
        *,
        collapse_manifest_equal_pairs: bool = False,
        manifest_report_mode: ManifestReportRenderMode = "full",
    ) -> dict[str, Any]:
        """序列化为可 JSON 化的字典结构。.

        Args:
            collapse_manifest_equal_pairs: 是否压缩内嵌 manifest_report 中
                `old_*` 与 `new_*` 相同的字段。
            manifest_report_mode: 内嵌 manifest_report 的渲染模式：
                - `full`: 原样保留完整 manifest_report（默认）
                - `summary`: 仅保留 manifest_report 的 `summary` 与 `moved`
                - `none`: 不输出 manifest_report
        """
        mode = _normalize_manifest_report_mode(manifest_report_mode)
        payload = asdict(self)
        manifest_report_payload = payload.get("manifest_report")
        if isinstance(manifest_report_payload, dict) and collapse_manifest_equal_pairs:
            _collapse_equal_manifest_fields(manifest_report_payload)

        if mode == "full":
            return payload
        if mode == "summary":
            if isinstance(manifest_report_payload, dict):
                payload["manifest_report"] = {
                    "summary": manifest_report_payload.get("summary"),
                    "moved": manifest_report_payload.get("moved", tuple()),
                }
            return payload
        if mode == "none":
            payload.pop("manifest_report", None)
            return payload
        return payload

    def to_pretty_json(
        self,
        *,
        indent: int = 2,
        ensure_ascii: bool = False,
        collapse_manifest_equal_pairs: bool = False,
        manifest_report_mode: ManifestReportRenderMode = "full",
    ) -> str:
        """返回美化后的 JSON 文本。.

        Args:
            indent: 缩进空格数。
            ensure_ascii: 是否仅输出 ASCII 字符。
            collapse_manifest_equal_pairs: 是否压缩内嵌 manifest_report 中
                `old_*` 与 `new_*` 相同字段。
            manifest_report_mode: manifest_report 输出模式；语义同 `to_dict`。

        Returns:
            JSON 字符串。
        """
        return json.dumps(
            self.to_dict(
                collapse_manifest_equal_pairs=collapse_manifest_equal_pairs,
                manifest_report_mode=manifest_report_mode,
            ),
            ensure_ascii=ensure_ascii,
            indent=indent,
            sort_keys=False,
        )

    def dump_pretty_json(
        self,
        output_path: str | PathLike[str],
        *,
        indent: int = 2,
        ensure_ascii: bool = False,
        collapse_manifest_equal_pairs: bool = False,
        manifest_report_mode: ManifestReportRenderMode = "full",
    ) -> str:
        """将报告写入本地 JSON 文件并返回写入路径。.

        Args:
            output_path: 输出文件路径。
            indent: 缩进空格数。
            ensure_ascii: 是否仅输出 ASCII 字符。
            collapse_manifest_equal_pairs: 是否压缩内嵌 manifest_report 中
                `old_*` 与 `new_*` 相同字段。
            manifest_report_mode: manifest_report 输出模式；语义同 `to_dict`。

        Returns:
            标准化后的输出路径字符串。
        """
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            self.to_pretty_json(
                indent=indent,
                ensure_ascii=ensure_ascii,
                collapse_manifest_equal_pairs=collapse_manifest_equal_pairs,
                manifest_report_mode=manifest_report_mode,
            ),
            encoding="utf-8",
        )
        return str(output)


def diff_wad_headers(
    old_manifest: ManifestInput | None = None,
    new_manifest: ManifestInput | None = None,
    *,
    manifest_report: ManifestDiffReport | None = None,
    flags: str | Iterable[str] | None = None,
    target_wad_files: Iterable[str] | None = None,
    inner_paths: Mapping[str, Iterable[str]] | Iterable[str] | None = None,
    include_unflagged_when_flags: bool = False,
    include_unchanged: bool = False,
    filter_source: Literal["both", "old", "new"] = "both",
    hash_type_mismatch_mode: HashTypeMismatchMode = "loose",
    overlap_warning_threshold: float = DEFAULT_OVERLAP_WARNING_THRESHOLD,
) -> WADHeaderDiffReport:
    """比较两个 Manifest 中 WAD 文件头部索引差异.

    Args:
        old_manifest: 旧版本 Manifest，支持 `PatcherManifest` 实例或 manifest 路径/URL。
            为空时会尝试从 `manifest_report` 里复用上一次 diff 的运行时上下文。
        new_manifest: 新版本 Manifest，支持 `PatcherManifest` 实例或 manifest 路径/URL。
            为空时会尝试从 `manifest_report` 里复用上一次 diff 的运行时上下文。
        manifest_report: 可选，已计算好的 Manifest diff 报告。
            传入后会复用该报告，不再重复执行 `diff_manifests`。
        flags: 按 flag 过滤目标 WAD。
        target_wad_files: 指定只比较的 WAD 路径列表（忽略大小写匹配）。
            该参数必须显式传入，避免对全量 WAD 触发大量额外网络请求。
        inner_paths: 可选内部路径聚焦过滤；可传全局路径列表或 `{wad_path: paths}` 映射。
        include_unflagged_when_flags: 启用 flags 过滤时是否保留无 flag 文件。
        include_unchanged: 是否包含未变化 WAD 的结果；关闭时也会过滤
            `section_diffs` 中 `status='unchanged'` 的内部条目。
        filter_source: 过滤基准来源，语义与 `diff_manifests` 相同。
        hash_type_mismatch_mode: 当 `chunk_hash_types` 不一致时的处理策略，语义与
            `diff_manifests` 相同。
        overlap_warning_threshold: Manifest 公共路径重叠率告警阈值。

    Returns:
        WAD 头部差异报告。

    Raises:
        ValueError: 未提供 `target_wad_files` 时抛出。
    """
    _validate_overlap_threshold(overlap_warning_threshold)
    old_manifest_obj, new_manifest_obj = _resolve_manifest_inputs(
        old_manifest=old_manifest,
        new_manifest=new_manifest,
        manifest_report=manifest_report,
    )

    requested_targets = _normalize_target_files(target_wad_files)
    if not requested_targets:
        raise ValueError("diff_wad_headers 必须显式提供 target_wad_files，建议先筛选后再执行 WAD 头部对比。")
    requested_wad_targets = {lowered: raw for lowered, raw in requested_targets.items() if lowered.endswith(".wad.client")}
    if not requested_wad_targets:
        raise ValueError("target_wad_files 中没有有效的 .wad.client 路径。")

    report = manifest_report
    if report is None:
        report = diff_manifests(
            old_manifest_obj,
            new_manifest_obj,
            pattern=WAD_FILE_PATTERN,
            flags=flags,
            target_files=requested_wad_targets.values(),
            include_unflagged_when_flags=include_unflagged_when_flags,
            include_unchanged=include_unchanged,
            detect_moves=False,
            filter_source=filter_source,
            hash_type_mismatch_mode=hash_type_mismatch_mode,
            overlap_warning_threshold=overlap_warning_threshold,
        )

    old_wads = _collect_target_files(old_manifest_obj, requested_wad_targets)
    new_wads = _collect_target_files(new_manifest_obj, requested_wad_targets)
    path_status = _build_wad_path_status(
        old_wads=old_wads,
        new_wads=new_wads,
        hash_type_mismatch_mode=hash_type_mismatch_mode,
    )

    file_results: list[WADFileDiffEntry] = []
    warnings = list(report.summary.warnings)

    old_extractor = WADExtractor(old_manifest_obj)
    new_extractor = WADExtractor(new_manifest_obj)
    try:
        for wad_path in sorted(path_status):
            status = path_status[wad_path]
            if status == "added":
                file_results.append(
                    WADFileDiffEntry(
                        wad_path=wad_path,
                        status="added",
                        section_diffs=tuple(),
                        missing_focused_paths=tuple(),
                        warning="该 WAD 仅在新版本存在，无法做头部双向对比。",
                    )
                )
                continue
            if status == "removed":
                file_results.append(
                    WADFileDiffEntry(
                        wad_path=wad_path,
                        status="removed",
                        section_diffs=tuple(),
                        missing_focused_paths=tuple(),
                        warning="该 WAD 仅在旧版本存在，无法做头部双向对比。",
                    )
                )
                continue
            if status == "unchanged" and not include_unchanged:
                continue

            old_file = old_wads[wad_path]
            new_file = new_wads[wad_path]
            try:
                old_header = old_extractor.get_wad_header(old_file)
                new_header = new_extractor.get_wad_header(new_file)
            except Exception as exc:  # noqa: BLE001
                file_results.append(
                    WADFileDiffEntry(
                        wad_path=wad_path,
                        status="error",
                        section_diffs=tuple(),
                        missing_focused_paths=tuple(),
                        warning=f"读取 WAD 头失败: {exc}",
                    )
                )
                continue

            focus_paths = _select_focus_inner_paths(wad_path, inner_paths)
            section_diffs, missing_focused_paths = _diff_wad_sections(
                old_header=old_header,
                new_header=new_header,
                focus_paths=focus_paths,
            )
            has_section_change = any(item.status != "unchanged" for item in section_diffs)
            visible_section_diffs = (
                section_diffs if include_unchanged else tuple(item for item in section_diffs if item.status != "unchanged")
            )
            wad_status: WADDiffStatus = "changed" if has_section_change else "unchanged"
            if status == "changed":
                wad_status = "changed"

            file_results.append(
                WADFileDiffEntry(
                    wad_path=wad_path,
                    status=wad_status,
                    section_diffs=visible_section_diffs,
                    missing_focused_paths=missing_focused_paths,
                )
            )
    finally:
        old_extractor.close()
        new_extractor.close()

    status_counts = _count_wad_status(file_results)
    summary = WADHeaderDiffSummary(
        total_wads=len(file_results),
        changed_count=status_counts["changed"],
        unchanged_count=status_counts["unchanged"],
        added_count=status_counts["added"],
        removed_count=status_counts["removed"],
        error_count=status_counts["error"],
        warnings=tuple(warnings),
    )
    sorted_files = tuple(sorted(file_results, key=lambda item: item.wad_path))
    enriched_manifest_report = attach_wad_sections_to_manifest_report(
        manifest_report=report,
        wad_files=sorted_files,
    )
    return WADHeaderDiffReport(
        summary=summary,
        files=sorted_files,
        manifest_report=enriched_manifest_report,
    )


def _resolve_manifest_inputs(
    *,
    old_manifest: ManifestInput | None,
    new_manifest: ManifestInput | None,
    manifest_report: ManifestDiffReport | None,
) -> tuple[PatcherManifest, PatcherManifest]:
    """解析 WAD diff 所需的 Manifest 实例."""
    if old_manifest is not None or new_manifest is not None:
        if old_manifest is None or new_manifest is None:
            raise ValueError("old_manifest/new_manifest 需同时提供，或都不提供并改为传入 manifest_report。")
        return _ensure_manifest(old_manifest), _ensure_manifest(new_manifest)

    if manifest_report is None:
        raise ValueError("必须提供 old_manifest/new_manifest，或传入可复用上下文的 manifest_report。")

    old_cached = getattr(manifest_report, "_old_manifest_obj", None)
    new_cached = getattr(manifest_report, "_new_manifest_obj", None)
    if isinstance(old_cached, PatcherManifest) and isinstance(new_cached, PatcherManifest):
        return old_cached, new_cached
    raise ValueError(
        "manifest_report 中没有可复用的 Manifest 上下文。"
        "请显式传入 old_manifest/new_manifest，或使用同进程内刚调用 diff_manifests 返回的报告对象。"
    )


def _build_wad_path_status(
    *,
    old_wads: Mapping[str, PatcherFile],
    new_wads: Mapping[str, PatcherFile],
    hash_type_mismatch_mode: HashTypeMismatchMode,
) -> dict[str, ManifestDiffStatus]:
    """按路径计算 WAD 文件级状态，用于控制后续头部 diff 输出."""
    result: dict[str, ManifestDiffStatus] = {}
    for path in sorted(set(old_wads) | set(new_wads)):
        old_file = old_wads.get(path)
        new_file = new_wads.get(path)
        if old_file is None and new_file is not None:
            result[path] = "added"
            continue
        if new_file is None and old_file is not None:
            result[path] = "removed"
            continue
        assert old_file is not None and new_file is not None  # nosec: B101 - 上方分支已保证
        changed_fields, _ = _diff_file_fields(
            old_file,
            new_file,
            hash_type_mismatch_mode=hash_type_mismatch_mode,
        )
        result[path] = "changed" if changed_fields else "unchanged"
    return result


def _select_focus_inner_paths(
    wad_path: str,
    inner_paths: Mapping[str, Iterable[str]] | Iterable[str] | None,
) -> tuple[str, ...]:
    if inner_paths is None:
        return tuple()
    if isinstance(inner_paths, Mapping):
        lowered_target = wad_path.lower()
        for mapping_key, values in inner_paths.items():
            if not isinstance(mapping_key, str):
                continue
            if mapping_key.lower() != lowered_target:
                continue
            return tuple(_normalize_paths(values))
        return tuple()
    return tuple(_normalize_paths(inner_paths))


def _normalize_paths(paths: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if not isinstance(path, str):
            continue
        cleaned = path.strip()
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        normalized.append(cleaned)
    return normalized


def _resolve_path_hash(header: Any, inner_path: str) -> int:
    version_hash_func = getattr(header, "_get_hash_for_path", None)
    if callable(version_hash_func):
        return int(version_hash_func(inner_path))
    fallback_hash_func = getattr(type(header), "get_hash", None)
    if callable(fallback_hash_func):
        return int(fallback_hash_func(inner_path))
    raise ValueError(f"当前 WAD 头对象不支持路径哈希计算: {type(header)!r}")


def _build_wad_section_map(
    header: Any,
    focus_hashes: set[int] | None = None,
) -> dict[int, tuple[WADSectionSignature, ...]]:
    section_index: dict[int, list[WADSectionSignature]] = {}
    for section in getattr(header, "files", ()):
        signature = WADSectionSignature.from_section(section)
        if focus_hashes is not None and signature.path_hash not in focus_hashes:
            continue
        section_index.setdefault(signature.path_hash, []).append(signature)

    normalized: dict[int, tuple[WADSectionSignature, ...]] = {}
    for path_hash, signatures in section_index.items():
        normalized[path_hash] = tuple(sorted(signatures, key=WADSectionSignature.sort_key))
    return normalized


def _diff_wad_sections(
    *,
    old_header: Any,
    new_header: Any,
    focus_paths: Sequence[str],
) -> tuple[tuple[WADSectionDiffEntry, ...], tuple[str, ...]]:
    focus_hashes: set[int] | None = None
    missing_focus_paths: list[str] = []

    if focus_paths:
        focus_hashes = set()
        for path in focus_paths:
            old_hash = _resolve_path_hash(old_header, path)
            new_hash = _resolve_path_hash(new_header, path)
            focus_hashes.add(old_hash)
            focus_hashes.add(new_hash)

    old_index = _build_wad_section_map(old_header, focus_hashes=focus_hashes)
    new_index = _build_wad_section_map(new_header, focus_hashes=focus_hashes)

    if focus_paths:
        for path in focus_paths:
            old_hash = _resolve_path_hash(old_header, path)
            new_hash = _resolve_path_hash(new_header, path)
            if (
                old_hash not in old_index
                and old_hash not in new_index
                and new_hash not in old_index
                and new_hash not in new_index
            ):
                missing_focus_paths.append(path)

    diffs: list[WADSectionDiffEntry] = []
    for path_hash in sorted(set(old_index) | set(new_index)):
        old_sections = old_index.get(path_hash, tuple())
        new_sections = new_index.get(path_hash, tuple())
        if not old_sections:
            status: ManifestDiffStatus = "added"
        elif not new_sections:
            status = "removed"
        elif old_sections == new_sections:
            status = "unchanged"
        else:
            status = "changed"
        diffs.append(
            WADSectionDiffEntry(
                path_hash=path_hash,
                status=status,
                old_sections=old_sections,
                new_sections=new_sections,
            )
        )
    return tuple(diffs), tuple(missing_focus_paths)


def _count_wad_status(entries: Sequence[WADFileDiffEntry]) -> dict[str, int]:
    counts = {"added": 0, "removed": 0, "changed": 0, "unchanged": 0, "error": 0}
    for entry in entries:
        counts[entry.status] += 1
    return counts


def _as_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def attach_wad_sections_to_manifest_report(
    *,
    manifest_report: ManifestDiffReport,
    wad_files: Sequence[WADFileDiffEntry],
) -> ManifestDiffReport:
    """将 WAD section_diffs 附加到对应的 ManifestDiffEntry."""
    wad_index = {item.wad_path.lower(): item for item in wad_files}
    if not wad_index:
        return manifest_report

    added_entries, added_changed = _attach_entries_with_wad_sections(manifest_report.added, wad_index)
    removed_entries, removed_changed = _attach_entries_with_wad_sections(manifest_report.removed, wad_index)
    changed_entries, changed_changed = _attach_entries_with_wad_sections(manifest_report.changed, wad_index)
    unchanged_entries, unchanged_changed = _attach_entries_with_wad_sections(manifest_report.unchanged, wad_index)

    if not any((added_changed, removed_changed, changed_changed, unchanged_changed)):
        return manifest_report

    updated_report = replace(
        manifest_report,
        added=added_entries,
        removed=removed_entries,
        changed=changed_entries,
        unchanged=unchanged_entries,
    )
    _copy_manifest_runtime_context(source=manifest_report, target=updated_report)
    return updated_report


def _attach_entries_with_wad_sections(
    entries: Sequence[ManifestDiffEntry],
    wad_index: Mapping[str, WADFileDiffEntry],
) -> tuple[tuple[ManifestDiffEntry, ...], bool]:
    """将单组 Manifest 条目与 wad section_diffs 做路径级关联."""
    updated_entries: list[ManifestDiffEntry] = []
    has_change = False
    for entry in entries:
        wad_file = wad_index.get(entry.path.lower())
        if wad_file is None:
            updated_entries.append(entry)
            continue
        if entry.section_diffs == wad_file.section_diffs:
            updated_entries.append(entry)
            continue
        updated_entries.append(
            replace(
                entry,
                section_diffs=wad_file.section_diffs,
            )
        )
        has_change = True
    return tuple(updated_entries), has_change


def _copy_manifest_runtime_context(
    *,
    source: ManifestDiffReport,
    target: ManifestDiffReport,
) -> None:
    """复制 Manifest 运行时上下文，保持后续流程可复用缓存对象."""
    old_manifest_obj = getattr(source, "_old_manifest_obj", None)
    new_manifest_obj = getattr(source, "_new_manifest_obj", None)
    object.__setattr__(target, "_old_manifest_obj", old_manifest_obj)
    object.__setattr__(target, "_new_manifest_obj", new_manifest_obj)


def _normalize_manifest_report_mode(mode: ManifestReportRenderMode) -> ManifestReportRenderMode:
    """规范化并校验 manifest_report 渲染模式."""
    allowed: set[ManifestReportRenderMode] = {"full", "summary", "none"}
    if mode not in allowed:
        raise ValueError(f"manifest_report_mode 仅支持 {sorted(allowed)}，当前值: {mode!r}")
    return mode
