"""Manifest 差异分析能力."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from os import PathLike
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from riotmanifest.diff.wad_header_diff import WADSectionDiffEntry

from riotmanifest.manifest import PatcherFile, PatcherManifest

ManifestDiffStatus = Literal["added", "removed", "changed", "unchanged"]
HashTypeMismatchMode = Literal["loose", "strict"]

DEFAULT_OVERLAP_WARNING_THRESHOLD = 0.10
ManifestInput = PatcherManifest | str | PathLike[str]
MANIFEST_ENTRY_EQUAL_FIELD_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("old_size", "new_size", "size"),
    ("old_flags", "new_flags", "flags"),
    ("old_link", "new_link", "link"),
    ("old_chunk_digest", "new_chunk_digest", "chunk_digest"),
)


@dataclass(frozen=True)
class ManifestDiffSummary:
    """Manifest 差异汇总信息."""

    total_old: int
    total_new: int
    total_common: int
    added_count: int
    removed_count: int
    changed_count: int
    unchanged_count: int
    overlap_ratio_old: float
    overlap_ratio_new: float
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ManifestDiffEntry:
    """单个 Manifest 文件差异条目."""

    path: str
    status: ManifestDiffStatus
    old_size: int | None
    new_size: int | None
    old_flags: tuple[str, ...] | None
    new_flags: tuple[str, ...] | None
    old_link: str | None
    new_link: str | None
    old_chunk_digest: str | None
    new_chunk_digest: str | None
    changed_fields: tuple[str, ...]
    section_diffs: tuple[WADSectionDiffEntry, ...] | None = None


@dataclass(frozen=True)
class ManifestMovedEntry:
    """推测的文件路径迁移条目（内容相同、路径变化）。."""

    old_path: str
    new_path: str
    size: int
    chunk_digest: str


@dataclass(frozen=True)
class ManifestDiffReport:
    """Manifest 差异分析结果."""

    summary: ManifestDiffSummary
    added: tuple[ManifestDiffEntry, ...]
    removed: tuple[ManifestDiffEntry, ...]
    changed: tuple[ManifestDiffEntry, ...]
    unchanged: tuple[ManifestDiffEntry, ...]
    moved: tuple[ManifestMovedEntry, ...]

    def to_dict(self, *, collapse_equal_pairs: bool = False) -> dict[str, Any]:
        """序列化为可 JSON 化的字典结构。.

        Args:
            collapse_equal_pairs: 是否将 `old_*` 与 `new_*` 相同的字段合并为单字段。
                例如 `old_size == new_size` 时输出 `size`，并移除原双字段。
        """
        payload = asdict(self)
        if collapse_equal_pairs:
            return _collapse_equal_manifest_fields(payload)
        return payload

    def to_pretty_json(
        self,
        *,
        indent: int = 2,
        ensure_ascii: bool = False,
        collapse_equal_pairs: bool = False,
    ) -> str:
        """返回美化后的 JSON 文本。.

        Args:
            indent: 缩进空格数。
            ensure_ascii: 是否仅输出 ASCII 字符。
            collapse_equal_pairs: 是否将 `old_*` 与 `new_*` 相同字段合并后再输出。

        Returns:
            JSON 字符串。
        """
        return json.dumps(
            self.to_dict(collapse_equal_pairs=collapse_equal_pairs),
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
        collapse_equal_pairs: bool = False,
    ) -> str:
        """将报告写入本地 JSON 文件并返回写入路径。.

        Args:
            output_path: 输出文件路径。
            indent: 缩进空格数。
            ensure_ascii: 是否仅输出 ASCII 字符。
            collapse_equal_pairs: 是否将 `old_*` 与 `new_*` 相同字段合并后再写入。

        Returns:
            标准化后的输出路径字符串。
        """
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            self.to_pretty_json(
                indent=indent,
                ensure_ascii=ensure_ascii,
                collapse_equal_pairs=collapse_equal_pairs,
            ),
            encoding="utf-8",
        )
        return str(output)


def _collapse_equal_manifest_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """合并 Manifest diff 条目中 `old_*` 与 `new_*` 相同的字段."""
    for section_name in ("added", "removed", "changed", "unchanged"):
        section_entries = payload.get(section_name)
        if not isinstance(section_entries, (list, tuple)):
            continue
        for entry in section_entries:
            if isinstance(entry, dict):
                _collapse_equal_field_pairs(entry)
    return payload


def _collapse_equal_field_pairs(entry: dict[str, Any]) -> None:
    """将单个 diff 条目里 old/new 成对且值相同的字段折叠为单字段."""
    for old_key, new_key, merged_key in MANIFEST_ENTRY_EQUAL_FIELD_PAIRS:
        if old_key not in entry or new_key not in entry:
            continue
        if entry[old_key] != entry[new_key]:
            continue
        entry[merged_key] = entry[old_key]
        del entry[old_key]
        del entry[new_key]

def diff_manifests(
    old_manifest: ManifestInput,
    new_manifest: ManifestInput,
    *,
    pattern: str | None = None,
    flags: str | Iterable[str] | None = None,
    target_files: Iterable[str] | None = None,
    include_unflagged_when_flags: bool = False,
    include_unchanged: bool = False,
    detect_moves: bool = True,
    filter_source: Literal["both", "old", "new"] = "both",
    hash_type_mismatch_mode: HashTypeMismatchMode = "loose",
    overlap_warning_threshold: float = DEFAULT_OVERLAP_WARNING_THRESHOLD,
) -> ManifestDiffReport:
    """比较两个 Manifest 的文件级差异.

    Args:
        old_manifest: 旧版本 Manifest，支持 `PatcherManifest` 实例或 manifest 路径/URL。
        new_manifest: 新版本 Manifest，支持 `PatcherManifest` 实例或 manifest 路径/URL。
        pattern: 文件名正则过滤（忽略大小写）。
        flags: 按 flag 过滤；支持字符串或字符串列表。
        target_files: 只比较指定路径集合（忽略大小写匹配）。
            当该参数非空时，会直接按路径定位文件并忽略 `pattern/flags`。
        include_unflagged_when_flags: 当启用 flags 过滤时，是否保留无 flag 文件。
        include_unchanged: 是否返回未变化文件条目。
        detect_moves: 是否对新增/删除条目执行“路径迁移”推测。
        filter_source: 过滤基准来源。`both` 表示两侧各自过滤；
            `old` 表示使用旧清单过滤出的路径去匹配新清单；
            `new` 表示使用新清单过滤出的路径去匹配旧清单。
        hash_type_mismatch_mode: 当 `chunk_hash_types` 不一致时的处理策略。
            `loose`（默认）：跳过 chunk_id 内容比较，避免跨 hash 算法迁移时误报；
            `strict`：按 chunk_id 与 hash_type 严格比较，兼容旧行为。
        overlap_warning_threshold: 公共路径重叠率告警阈值。

    Returns:
        差异报告对象。
    """
    old_manifest_obj = _ensure_manifest(old_manifest)
    new_manifest_obj = _ensure_manifest(new_manifest)

    _validate_filter_source(filter_source)
    _validate_hash_type_mismatch_mode(hash_type_mismatch_mode)
    _validate_overlap_threshold(overlap_warning_threshold)
    effective_include_unchanged = include_unchanged or bool(target_files)

    requested_targets = _normalize_target_files(target_files)
    warnings: list[str] = []
    if requested_targets:
        old_files = _collect_target_files(old_manifest_obj, requested_targets)
        new_files = _collect_target_files(new_manifest_obj, requested_targets)
        warnings_missing_targets = _build_missing_target_warnings(requested_targets, old_files, new_files)
        if filter_source != "both":
            warnings.append("已提供 target_files，filter_source 将被忽略。")
    else:
        old_filtered = _collect_filtered_files(
            old_manifest_obj,
            pattern=pattern,
            flags=flags,
            include_unflagged_when_flags=include_unflagged_when_flags,
        )
        new_filtered = _collect_filtered_files(
            new_manifest_obj,
            pattern=pattern,
            flags=flags,
            include_unflagged_when_flags=include_unflagged_when_flags,
        )

        old_files, new_files = _align_filtered_sets(
            old_manifest=old_manifest_obj,
            new_manifest=new_manifest_obj,
            old_filtered=old_filtered,
            new_filtered=new_filtered,
            filter_source=filter_source,
        )
        warnings_missing_targets = []

    old_names = set(old_files)
    new_names = set(new_files)
    common_names = old_names & new_names

    warnings.extend(_build_overlap_warnings(old_names, new_names, overlap_warning_threshold))
    warnings.extend(warnings_missing_targets)

    added_entries: list[ManifestDiffEntry] = []
    removed_entries: list[ManifestDiffEntry] = []
    changed_entries: list[ManifestDiffEntry] = []
    unchanged_entries: list[ManifestDiffEntry] = []
    skipped_chunk_compare_count = 0

    for path in sorted(old_names | new_names):
        old_file = old_files.get(path)
        new_file = new_files.get(path)

        if old_file is None and new_file is not None:
            added_entries.append(_build_manifest_entry(path=path, status="added", old_file=None, new_file=new_file))
            continue
        if new_file is None and old_file is not None:
            removed_entries.append(_build_manifest_entry(path=path, status="removed", old_file=old_file, new_file=None))
            continue

        assert old_file is not None and new_file is not None  # nosec: B101 - 已由分支条件保证
        changed_fields, skipped_chunk_compare = _diff_file_fields(
            old_file,
            new_file,
            hash_type_mismatch_mode=hash_type_mismatch_mode,
        )
        if skipped_chunk_compare:
            skipped_chunk_compare_count += 1
        if changed_fields:
            changed_entries.append(
                _build_manifest_entry(
                    path=path,
                    status="changed",
                    old_file=old_file,
                    new_file=new_file,
                    changed_fields=changed_fields,
                )
            )
        elif effective_include_unchanged:
            unchanged_entries.append(_build_manifest_entry(path=path, status="unchanged", old_file=old_file, new_file=new_file))

    moved_entries: list[ManifestMovedEntry] = []
    if detect_moves and added_entries and removed_entries:
        moved_entries = _detect_moved_entries(added_entries=added_entries, removed_entries=removed_entries)
    if skipped_chunk_compare_count > 0 and hash_type_mismatch_mode == "loose":
        warnings.append(
            "存在 chunk_hash_types 不一致的文件，已跳过 chunk_id 内容比较以避免误报。"
            f"count={skipped_chunk_compare_count}。如需严格比较请设置 hash_type_mismatch_mode='strict'。"
        )

    summary = ManifestDiffSummary(
        total_old=len(old_files),
        total_new=len(new_files),
        total_common=len(common_names),
        added_count=len(added_entries),
        removed_count=len(removed_entries),
        changed_count=len(changed_entries),
        unchanged_count=len(unchanged_entries),
        overlap_ratio_old=(len(common_names) / len(old_names)) if old_names else 1.0,
        overlap_ratio_new=(len(common_names) / len(new_names)) if new_names else 1.0,
        warnings=tuple(warnings),
    )
    report = ManifestDiffReport(
        summary=summary,
        added=tuple(added_entries),
        removed=tuple(removed_entries),
        changed=tuple(changed_entries),
        unchanged=tuple(unchanged_entries),
        moved=tuple(moved_entries),
    )
    _attach_manifest_runtime_context(report, old_manifest_obj, new_manifest_obj)
    return report


def _compile_pattern(pattern: str | None) -> re.Pattern[str] | None:
    if pattern is None or not pattern.strip():
        return None
    return re.compile(pattern, re.IGNORECASE)


def _normalize_flags(flags: str | Iterable[str] | None) -> set[str] | None:
    if flags is None:
        return None
    if isinstance(flags, str):
        return {flags.lower()}
    normalized = {flag.lower() for flag in flags if isinstance(flag, str) and flag.strip()}
    return normalized or None


def _normalize_target_files(target_files: Iterable[str] | None) -> dict[str, str]:
    if target_files is None:
        return {}
    normalized: dict[str, str] = {}
    for path in target_files:
        if not isinstance(path, str):
            continue
        clean = path.strip()
        if not clean:
            continue
        normalized.setdefault(clean.lower(), clean)
    return normalized


def _collect_filtered_files(
    manifest: PatcherManifest,
    *,
    pattern: str | None,
    flags: str | Iterable[str] | None,
    include_unflagged_when_flags: bool,
) -> dict[str, PatcherFile]:
    result = {file_item.name: file_item for file_item in manifest.filter_files(pattern=pattern, flag=flags)}
    if not include_unflagged_when_flags or flags is None:
        return result

    compiled_pattern = _compile_pattern(pattern)
    for file_item in manifest.files.values():
        if file_item.flags is not None:
            continue
        if compiled_pattern is not None and not compiled_pattern.search(file_item.name):
            continue
        result.setdefault(file_item.name, file_item)
    return result


def _select_by_names(source: Mapping[str, PatcherFile], selected_names: Iterable[str]) -> dict[str, PatcherFile]:
    name_index = {name.lower() for name in selected_names}
    return {name: file_item for name, file_item in source.items() if name.lower() in name_index}


def _collect_target_files(
    manifest: PatcherManifest,
    requested_targets: Mapping[str, str],
) -> dict[str, PatcherFile]:
    if not requested_targets:
        return {}

    result: dict[str, PatcherFile] = {}
    for name, file_item in manifest.files.items():
        if name.lower() in requested_targets:
            result[name] = file_item
    return result


def _align_filtered_sets(
    *,
    old_manifest: PatcherManifest,
    new_manifest: PatcherManifest,
    old_filtered: Mapping[str, PatcherFile],
    new_filtered: Mapping[str, PatcherFile],
    filter_source: Literal["both", "old", "new"],
) -> tuple[dict[str, PatcherFile], dict[str, PatcherFile]]:
    if filter_source == "both":
        return dict(old_filtered), dict(new_filtered)
    if filter_source == "old":
        selected_names = old_filtered.keys()
        return dict(old_filtered), _select_by_names(new_manifest.files, selected_names)
    selected_names = new_filtered.keys()
    return _select_by_names(old_manifest.files, selected_names), dict(new_filtered)


def _flags_tuple(file_item: PatcherFile | None) -> tuple[str, ...] | None:
    if file_item is None or file_item.flags is None:
        return None
    return tuple(sorted(file_item.flags))


def _chunk_digest(file_item: PatcherFile | None) -> str | None:
    if file_item is None:
        return None
    return file_item.hexdigest()


def _build_manifest_entry(
    *,
    path: str,
    status: ManifestDiffStatus,
    old_file: PatcherFile | None,
    new_file: PatcherFile | None,
    changed_fields: Sequence[str] = (),
) -> ManifestDiffEntry:
    return ManifestDiffEntry(
        path=path,
        status=status,
        old_size=old_file.size if old_file is not None else None,
        new_size=new_file.size if new_file is not None else None,
        old_flags=_flags_tuple(old_file),
        new_flags=_flags_tuple(new_file),
        old_link=old_file.link if old_file is not None else None,
        new_link=new_file.link if new_file is not None else None,
        old_chunk_digest=_chunk_digest(old_file),
        new_chunk_digest=_chunk_digest(new_file),
        changed_fields=tuple(changed_fields),
    )


def _chunk_hash_type_signature(file_item: PatcherFile) -> tuple[int, ...]:
    hash_types = {file_item.chunk_hash_types.get(chunk.chunk_id, 0) for chunk in file_item.chunks}
    return tuple(sorted(hash_types))


def _diff_file_fields(
    old_file: PatcherFile,
    new_file: PatcherFile,
    *,
    hash_type_mismatch_mode: HashTypeMismatchMode,
) -> tuple[tuple[str, ...], bool]:
    changed_fields: list[str] = []
    skipped_chunk_compare = False
    if old_file.size != new_file.size:
        changed_fields.append("size")
    if (old_file.link or "") != (new_file.link or ""):
        changed_fields.append("link")
    if _flags_tuple(old_file) != _flags_tuple(new_file):
        changed_fields.append("flags")

    old_hash_type_signature = _chunk_hash_type_signature(old_file)
    new_hash_type_signature = _chunk_hash_type_signature(new_file)
    hash_type_same = old_hash_type_signature == new_hash_type_signature
    if not hash_type_same and hash_type_mismatch_mode == "strict":
        changed_fields.append("chunk_hash_types")

    if hash_type_same or hash_type_mismatch_mode == "strict":
        if old_file.hexdigest() != new_file.hexdigest():
            changed_fields.append("chunks")
    else:
        skipped_chunk_compare = True

    return tuple(changed_fields), skipped_chunk_compare


def _build_overlap_warnings(old_names: set[str], new_names: set[str], threshold: float) -> list[str]:
    if not old_names or not new_names:
        return []

    common_count = len(old_names & new_names)
    overlap_old = common_count / len(old_names)
    overlap_new = common_count / len(new_names)
    if common_count == 0:
        return [
            "两个 Manifest 在筛选后没有任何公共路径，可能是跨游戏清单，或发生了极大规模路径迁移。",
        ]
    if min(overlap_old, overlap_new) < threshold:
        return [
            (
                "两个 Manifest 的公共路径重叠率较低，diff 结论参考价值可能下降。"
                f"overlap_old={overlap_old:.3f}, overlap_new={overlap_new:.3f}"
            )
        ]
    return []


def _build_missing_target_warnings(
    requested_targets: Mapping[str, str],
    old_files: Mapping[str, PatcherFile],
    new_files: Mapping[str, PatcherFile],
) -> list[str]:
    if not requested_targets:
        return []
    existing = {name.lower() for name in old_files} | {name.lower() for name in new_files}
    missing = [raw for lowered, raw in requested_targets.items() if lowered not in existing]
    if not missing:
        return []
    preview = ", ".join(sorted(missing)[:5])
    suffix = "" if len(missing) <= 5 else f" ... 共{len(missing)}个"
    return [f"指定目标文件在两侧 Manifest 中都不存在: {preview}{suffix}"]


def _fingerprint(entry: ManifestDiffEntry) -> tuple[int, str, str] | None:
    size = entry.new_size if entry.new_size is not None else entry.old_size
    link = entry.new_link if entry.new_link is not None else entry.old_link
    digest = entry.new_chunk_digest if entry.new_chunk_digest is not None else entry.old_chunk_digest
    if size is None or link is None or digest is None:
        return None
    return size, link, digest


def _detect_moved_entries(
    *,
    added_entries: Sequence[ManifestDiffEntry],
    removed_entries: Sequence[ManifestDiffEntry],
) -> list[ManifestMovedEntry]:
    added_by_fp: dict[tuple[int, str, str], list[ManifestDiffEntry]] = {}
    removed_by_fp: dict[tuple[int, str, str], list[ManifestDiffEntry]] = {}
    for entry in added_entries:
        fingerprint = _fingerprint(entry)
        if fingerprint is not None:
            added_by_fp.setdefault(fingerprint, []).append(entry)
    for entry in removed_entries:
        fingerprint = _fingerprint(entry)
        if fingerprint is not None:
            removed_by_fp.setdefault(fingerprint, []).append(entry)

    moved: list[ManifestMovedEntry] = []
    for fingerprint, removed_group in removed_by_fp.items():
        added_group = added_by_fp.get(fingerprint)
        if added_group is None:
            continue
        if len(removed_group) != 1 or len(added_group) != 1:
            continue
        removed_entry = removed_group[0]
        added_entry = added_group[0]
        if removed_entry.path == added_entry.path:
            continue
        moved.append(
            ManifestMovedEntry(
                old_path=removed_entry.path,
                new_path=added_entry.path,
                size=fingerprint[0],
                chunk_digest=fingerprint[2],
            )
        )
    moved.sort(key=lambda item: (item.old_path, item.new_path))
    return moved


def _ensure_manifest(manifest: ManifestInput) -> PatcherManifest:
    """把 manifest 入参统一转换为 `PatcherManifest` 实例.

    Args:
        manifest: `PatcherManifest` 或 manifest 路径/URL。

    Returns:
        可直接用于差异分析的 `PatcherManifest` 实例。

    Raises:
        TypeError: 入参类型不支持时抛出。
    """
    if isinstance(manifest, PatcherManifest):
        return manifest
    if isinstance(manifest, (str, PathLike)):
        return PatcherManifest(file=manifest, path="")
    raise TypeError(
        "manifest 参数必须是 PatcherManifest 实例或 manifest 路径/URL，"
        f"当前类型: {type(manifest)!r}"
    )


def _validate_overlap_threshold(threshold: float) -> None:
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(f"overlap_warning_threshold 必须在 [0, 1] 区间内，当前为 {threshold}")


def _validate_filter_source(filter_source: str) -> None:
    if filter_source not in {"both", "old", "new"}:
        raise ValueError(f"filter_source 仅支持 both/old/new，当前为 {filter_source}")


def _validate_hash_type_mismatch_mode(mode: str) -> None:
    if mode not in {"loose", "strict"}:
        raise ValueError(f"hash_type_mismatch_mode 仅支持 loose/strict，当前为 {mode}")


def _attach_manifest_runtime_context(
    report: ManifestDiffReport,
    old_manifest: PatcherManifest,
    new_manifest: PatcherManifest,
) -> None:
    """为报告挂载运行期上下文，供后续 WAD 头部 diff 复用 Manifest 实例."""
    object.__setattr__(report, "_old_manifest_obj", old_manifest)
    object.__setattr__(report, "_new_manifest_obj", new_manifest)
