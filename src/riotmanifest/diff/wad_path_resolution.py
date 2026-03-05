"""基于 BIN 的 WAD section 路径回填能力."""

from __future__ import annotations

import asyncio
import re
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import replace
from os import PathLike
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from league_tools.formats import BIN, WAD

from riotmanifest.diff.manifest_diff import ManifestDiffStatus
from riotmanifest.diff.path_providers import WADPathProvider
from riotmanifest.diff.wad_header_diff import (
    WADFileDiffEntry,
    WADHeaderDiffReport,
    WADSectionDiffEntry,
    attach_wad_sections_to_manifest_report,
)
from riotmanifest.extractor.wad_extractor import WADExtractor
from riotmanifest.manifest import PatcherFile, PatcherManifest

ManifestLike = PatcherManifest | str | PathLike[str]
BinDataSourceMode = Literal["extractor", "download_root_wad"]
_LOCALE_SEGMENT_PATTERN = re.compile(r"^[a-z]{2}_[a-z]{2}$", re.IGNORECASE)
_MAP_WAD_PATH_SEGMENT = "/maps/"
_DEFAULT_ROOT_WAD_CACHE_DIR = Path(".cache") / "wad_root_wad_bin_resolution"


class _DownloadedRootWadStore:
    """存放已下载 root WAD 本地路径并管理清理生命周期."""

    def __init__(
        self,
        *,
        old_paths: Mapping[str, Path],
        new_paths: Mapping[str, Path],
        cleanup_root: Path | None,
    ):
        self.old_paths = dict(old_paths)
        self.new_paths = dict(new_paths)
        self.cleanup_root = cleanup_root

    def get_local_path(self, *, manifest_side: Literal["old", "new"], wad_key: str) -> Path | None:
        """按 manifest 侧与 wad key 获取本地 WAD 路径."""
        normalized_key = wad_key.lower()
        if manifest_side == "old":
            return self.old_paths.get(normalized_key)
        return self.new_paths.get(normalized_key)

    def close(self) -> None:
        """清理临时下载目录."""
        if self.cleanup_root is None:
            return
        shutil.rmtree(self.cleanup_root, ignore_errors=True)


def resolve_wad_diff_paths(
    wad_report: WADHeaderDiffReport,
    *,
    path_provider: WADPathProvider,
    old_manifest: ManifestLike | None = None,
    new_manifest: ManifestLike | None = None,
    include_section_statuses: Iterable[ManifestDiffStatus] | None = None,
    bin_data_source_mode: BinDataSourceMode = "extractor",
    root_wad_download_dir: str | PathLike[str] | None = None,
    cleanup_downloaded_root_wads: bool = True,
    download_map_root_wads: bool = False,
    root_wad_download_concurrency_limit: int | None = None,
) -> WADHeaderDiffReport:
    """用 BIN 的 `bank_units.bank_path` 回填 `WADSectionDiffEntry.path`.

    Args:
        wad_report: 已计算好的 WAD 头部 diff 报告。
        path_provider: BIN 种子路径提供器，实现 `collect_paths(wad_path)`。
        old_manifest: 可选旧 manifest；不传时尝试复用 `wad_report` 运行时上下文。
        new_manifest: 可选新 manifest；不传时尝试复用 `wad_report` 运行时上下文。
        include_section_statuses: 仅回填指定状态的 section。
            不传时默认处理 `added/removed/changed`。
        bin_data_source_mode: BIN 数据来源模式：
            - `extractor`: 使用 `WADExtractor.extract_files` 按需提取（默认）。
            - `download_root_wad`: 优先下载 root WAD 到本地再解析 BIN。
        root_wad_download_dir: `download_root_wad` 模式的本地缓存目录。
            不传时默认使用 `./.cache/wad_root_wad_bin_resolution`。
        cleanup_downloaded_root_wads: 是否在本次回填结束后清理下载的 root WAD。
            默认开启，异常时也会清理。
        download_map_root_wads: `download_root_wad` 模式下是否对地图类 WAD 也走整包下载。
            默认 `False`，地图仍走 extractor 按需提取。
        root_wad_download_concurrency_limit: root WAD 整包下载并发上限。
            不传则使用 manifest 默认并发。

    Returns:
        与输入同结构的新 `WADHeaderDiffReport`；可匹配项会回填到
        `WADSectionDiffEntry.path`，未匹配保持 `None`。

    Raises:
        TypeError: `path_provider` 不符合协议时抛出。
        ValueError: manifest 上下文不可用或参数非法时抛出。
    """
    if not isinstance(path_provider, WADPathProvider):
        raise TypeError("path_provider 必须实现 collect_paths(wad_path) 协议。")

    old_manifest_obj, new_manifest_obj = _resolve_manifest_context(
        wad_report=wad_report,
        old_manifest=old_manifest,
        new_manifest=new_manifest,
    )
    section_status_filter = _normalize_section_statuses(include_section_statuses)

    old_files = _build_manifest_file_index(old_manifest_obj)
    new_files = _build_manifest_file_index(new_manifest_obj)
    normalized_mode = _normalize_bin_data_source_mode(bin_data_source_mode)

    resolved_files: list[WADFileDiffEntry] = []
    old_extractor = WADExtractor(old_manifest_obj)
    new_extractor = WADExtractor(new_manifest_obj)
    downloaded_root_store = _prepare_downloaded_root_wad_store(
        wad_report=wad_report,
        old_manifest=old_manifest_obj,
        new_manifest=new_manifest_obj,
        old_files=old_files,
        new_files=new_files,
        section_status_filter=section_status_filter,
        mode=normalized_mode,
        root_wad_download_dir=root_wad_download_dir,
        cleanup_downloaded_root_wads=cleanup_downloaded_root_wads,
        download_map_root_wads=download_map_root_wads,
        concurrency_limit=root_wad_download_concurrency_limit,
    )
    try:
        for file_entry in wad_report.files:
            resolved_files.append(
                _resolve_single_wad_paths(
                    file_entry=file_entry,
                    path_provider=path_provider,
                    old_extractor=old_extractor,
                    new_extractor=new_extractor,
                    old_files=old_files,
                    new_files=new_files,
                    section_status_filter=section_status_filter,
                    bin_data_source_mode=normalized_mode,
                    downloaded_root_store=downloaded_root_store,
                    download_map_root_wads=download_map_root_wads,
                )
            )
    finally:
        old_extractor.close()
        new_extractor.close()
        downloaded_root_store.close()

    resolved_files_tuple = tuple(resolved_files)
    enriched_manifest_report = attach_wad_sections_to_manifest_report(
        manifest_report=wad_report.manifest_report,
        wad_files=resolved_files_tuple,
    )
    return replace(
        wad_report,
        files=resolved_files_tuple,
        manifest_report=enriched_manifest_report,
    )


def _resolve_single_wad_paths(
    *,
    file_entry: WADFileDiffEntry,
    path_provider: WADPathProvider,
    old_extractor: WADExtractor,
    new_extractor: WADExtractor,
    old_files: dict[str, PatcherFile],
    new_files: dict[str, PatcherFile],
    section_status_filter: set[ManifestDiffStatus],
    bin_data_source_mode: BinDataSourceMode,
    downloaded_root_store: _DownloadedRootWadStore,
    download_map_root_wads: bool,
) -> WADFileDiffEntry:
    """解析单个 WAD 的 section 路径并返回新条目."""
    pending_hashes = {
        section.path_hash
        for section in file_entry.section_diffs
        if section.status in section_status_filter and section.path is None
    }
    if not pending_hashes:
        return file_entry

    region_wad_key = file_entry.wad_path.lower()
    bin_source_wad_key = _resolve_bin_source_wad_path(file_entry.wad_path)

    candidate_bin_paths = path_provider.collect_paths(bin_source_wad_key)
    normalized_bin_paths = _normalize_paths(candidate_bin_paths)
    if not normalized_bin_paths:
        return file_entry

    old_region_file = old_files.get(region_wad_key)
    new_region_file = new_files.get(region_wad_key)
    old_bin_source_file = old_files.get(bin_source_wad_key)
    new_bin_source_file = new_files.get(bin_source_wad_key)

    old_header = _load_wad_header(old_extractor, old_region_file)
    new_header = _load_wad_header(new_extractor, new_region_file)

    hash_to_path: dict[int, str] = {}
    if old_bin_source_file is not None:
        old_bank_paths = _collect_bank_paths(
            extractor=old_extractor,
            wad_file=old_bin_source_file,
            bin_paths=normalized_bin_paths,
            mode=bin_data_source_mode,
            local_wad_path=downloaded_root_store.get_local_path(
                manifest_side="old",
                wad_key=bin_source_wad_key,
            ),
            wad_key=bin_source_wad_key,
            download_map_root_wads=download_map_root_wads,
        )
        _merge_hash_mapping(
            hash_to_path=hash_to_path,
            real_paths=old_bank_paths,
            target_hashes=pending_hashes,
            primary_header=old_header,
            secondary_header=new_header,
        )

    if new_bin_source_file is not None:
        new_bank_paths = _collect_bank_paths(
            extractor=new_extractor,
            wad_file=new_bin_source_file,
            bin_paths=normalized_bin_paths,
            mode=bin_data_source_mode,
            local_wad_path=downloaded_root_store.get_local_path(
                manifest_side="new",
                wad_key=bin_source_wad_key,
            ),
            wad_key=bin_source_wad_key,
            download_map_root_wads=download_map_root_wads,
        )
        _merge_hash_mapping(
            hash_to_path=hash_to_path,
            real_paths=new_bank_paths,
            target_hashes=pending_hashes,
            primary_header=new_header,
            secondary_header=old_header,
        )

    if not hash_to_path:
        return file_entry

    updated_sections: list[WADSectionDiffEntry] = []
    has_update = False
    for section in file_entry.section_diffs:
        if section.path is not None or section.status not in section_status_filter:
            updated_sections.append(section)
            continue
        resolved_path = hash_to_path.get(section.path_hash)
        if resolved_path is None:
            updated_sections.append(section)
            continue
        updated_sections.append(replace(section, path=resolved_path))
        has_update = True

    if not has_update:
        return file_entry
    return replace(file_entry, section_diffs=tuple(updated_sections))


def _load_wad_header(extractor: WADExtractor, wad_file: PatcherFile | None) -> Any | None:
    """尽力读取 WAD 头；失败时返回 `None`."""
    if wad_file is None:
        return None
    try:
        return extractor.get_wad_header(wad_file)
    except Exception:  # noqa: BLE001
        return None


def _collect_bank_paths(
    *,
    extractor: WADExtractor,
    wad_file: PatcherFile,
    bin_paths: tuple[str, ...],
    mode: BinDataSourceMode,
    local_wad_path: Path | None,
    wad_key: str,
    download_map_root_wads: bool,
) -> tuple[str, ...]:
    """按配置模式收集 BIN 中的 bank_path."""
    if not bin_paths:
        return tuple()

    if mode == "download_root_wad":
        can_use_local = (
            local_wad_path is not None
            and (download_map_root_wads or not _is_map_wad_path(wad_key))
        )
        if can_use_local:
            return _collect_bank_paths_from_local_wad_bins(
                local_wad_path=local_wad_path,
                bin_paths=bin_paths,
            )

    return _collect_bank_paths_from_wad_bins(
        extractor=extractor,
        wad_file=wad_file,
        bin_paths=bin_paths,
    )


def _collect_bank_paths_from_local_wad_bins(
    *,
    local_wad_path: Path,
    bin_paths: tuple[str, ...],
) -> tuple[str, ...]:
    """从本地已下载的 WAD 文件解析 BIN 并提取 bank_path."""
    if not bin_paths or not local_wad_path.is_file():
        return tuple()

    try:
        local_wad = WAD(local_wad_path)
        extracted = local_wad.extract(list(bin_paths), raw=True)
    except Exception:  # noqa: BLE001
        return tuple()

    real_paths: list[str] = []
    for data in extracted:
        if not isinstance(data, (bytes, bytearray)):
            continue
        real_paths.extend(_parse_bin_bank_paths(bytes(data)))
    return _normalize_paths(real_paths)


def _collect_bank_paths_from_wad_bins(
    *,
    extractor: WADExtractor,
    wad_file: PatcherFile,
    bin_paths: tuple[str, ...],
) -> tuple[str, ...]:
    """从目标 WAD 的 BIN 文件中提取 `bank_units.bank_path` 路径."""
    if not bin_paths:
        return tuple()

    extracted = extractor.extract_files({wad_file.name: list(bin_paths)})
    wad_payload = extracted.get(wad_file.name, {})

    real_paths: list[str] = []
    for data in wad_payload.values():
        if not isinstance(data, (bytes, bytearray)):
            continue
        real_paths.extend(_parse_bin_bank_paths(bytes(data)))
    return _normalize_paths(real_paths)


def _parse_bin_bank_paths(bin_data: bytes) -> tuple[str, ...]:
    """解析单个 BIN 字节并返回 bank 路径集合."""
    try:
        parsed = BIN(bin_data)
    except Exception:  # noqa: BLE001
        return tuple()

    bank_paths: list[str] = []
    for bank_unit in _iter_bank_units(parsed.data):
        bank_paths.extend(_extract_bank_paths(bank_unit))
    return _normalize_paths(bank_paths)


def _iter_bank_units(data: Any) -> Iterable[Any]:
    """遍历 BIN `data` 中可识别的 bank unit 节点."""
    if data is None:
        return tuple()

    values: Iterable[Any]
    if isinstance(data, Mapping):
        values = data.values()
    elif isinstance(data, Iterable) and not isinstance(data, (str, bytes, bytearray)):
        values = data
    else:
        values = (data,)

    units: list[Any] = []
    for item in values:
        bank_units = getattr(item, "bank_units", None)
        if bank_units is None and isinstance(item, Mapping):
            bank_units = item.get("bank_units")
        if bank_units is None:
            continue

        if isinstance(bank_units, Mapping):
            units.extend(bank_units.values())
            continue

        if isinstance(bank_units, Iterable) and not isinstance(bank_units, (str, bytes, bytearray)):
            units.extend(bank_units)
            continue

        units.append(bank_units)

    return tuple(units)


def _extract_bank_paths(bank_unit: Any) -> tuple[str, ...]:
    """从单个 bank unit 提取 `bank_path` 路径列表."""
    raw_paths = bank_unit.get("bank_path") if isinstance(bank_unit, Mapping) else getattr(bank_unit, "bank_path", None)

    if raw_paths is None:
        return tuple()
    if isinstance(raw_paths, str):
        return _normalize_paths((raw_paths,))
    if isinstance(raw_paths, Iterable) and not isinstance(raw_paths, (bytes, bytearray)):
        return _normalize_paths(raw_paths)
    return tuple()


def _merge_hash_mapping(
    *,
    hash_to_path: dict[int, str],
    real_paths: tuple[str, ...],
    target_hashes: set[int],
    primary_header: Any | None,
    secondary_header: Any | None,
) -> None:
    """把真实路径按 hash 映射到目标 section."""
    for path in real_paths:
        path_hash = _safe_resolve_hash(primary_header, path)
        if path_hash is None:
            path_hash = _safe_resolve_hash(secondary_header, path)
        if path_hash is None:
            continue
        if path_hash not in target_hashes:
            continue
        hash_to_path.setdefault(path_hash, path)


def _safe_resolve_hash(header: Any | None, path: str) -> int | None:
    """在 header 可用时计算路径 hash."""
    if header is None:
        return None

    version_hash_func = getattr(header, "_get_hash_for_path", None)
    if callable(version_hash_func):
        return int(version_hash_func(path))

    fallback_hash_func = getattr(type(header), "get_hash", None)
    if callable(fallback_hash_func):
        return int(fallback_hash_func(path))

    return None


def _normalize_bin_data_source_mode(mode: BinDataSourceMode) -> BinDataSourceMode:
    """规范化 BIN 数据来源模式."""
    allowed: set[BinDataSourceMode] = {"extractor", "download_root_wad"}
    if mode not in allowed:
        raise ValueError(f"bin_data_source_mode 仅支持 {sorted(allowed)}，当前值: {mode!r}")
    return mode


def _prepare_downloaded_root_wad_store(
    *,
    wad_report: WADHeaderDiffReport,
    old_manifest: PatcherManifest,
    new_manifest: PatcherManifest,
    old_files: dict[str, PatcherFile],
    new_files: dict[str, PatcherFile],
    section_status_filter: set[ManifestDiffStatus],
    mode: BinDataSourceMode,
    root_wad_download_dir: str | PathLike[str] | None,
    cleanup_downloaded_root_wads: bool,
    download_map_root_wads: bool,
    concurrency_limit: int | None,
) -> _DownloadedRootWadStore:
    """按模式准备可复用的 root WAD 本地下载结果."""
    if mode != "download_root_wad":
        return _DownloadedRootWadStore(old_paths={}, new_paths={}, cleanup_root=None)

    old_targets, new_targets = _collect_root_wad_download_targets(
        wad_report=wad_report,
        old_files=old_files,
        new_files=new_files,
        section_status_filter=section_status_filter,
        download_map_root_wads=download_map_root_wads,
    )
    if not old_targets and not new_targets:
        return _DownloadedRootWadStore(old_paths={}, new_paths={}, cleanup_root=None)

    run_root, cleanup_root = _resolve_root_wad_download_layout(
        root_wad_download_dir=root_wad_download_dir,
        cleanup_downloaded_root_wads=cleanup_downloaded_root_wads,
    )
    old_root = run_root / "old"
    new_root = run_root / "new"
    old_root.mkdir(parents=True, exist_ok=True)
    new_root.mkdir(parents=True, exist_ok=True)

    try:
        old_paths = _download_root_wads_for_manifest(
            manifest=old_manifest,
            targets=old_targets,
            output_root=old_root,
            concurrency_limit=concurrency_limit,
        )
        new_paths = _download_root_wads_for_manifest(
            manifest=new_manifest,
            targets=new_targets,
            output_root=new_root,
            concurrency_limit=concurrency_limit,
        )
    except Exception:  # noqa: BLE001
        if cleanup_root is not None:
            shutil.rmtree(cleanup_root, ignore_errors=True)
        raise

    return _DownloadedRootWadStore(
        old_paths=old_paths,
        new_paths=new_paths,
        cleanup_root=cleanup_root,
    )


def _collect_root_wad_download_targets(
    *,
    wad_report: WADHeaderDiffReport,
    old_files: dict[str, PatcherFile],
    new_files: dict[str, PatcherFile],
    section_status_filter: set[ManifestDiffStatus],
    download_map_root_wads: bool,
) -> tuple[dict[str, PatcherFile], dict[str, PatcherFile]]:
    """收集需要整包下载的 root WAD（按 old/new 分组）."""
    old_targets: dict[str, PatcherFile] = {}
    new_targets: dict[str, PatcherFile] = {}

    for file_entry in wad_report.files:
        if not _has_pending_sections(file_entry, section_status_filter):
            continue
        bin_source_wad_key = _resolve_bin_source_wad_path(file_entry.wad_path)
        if not download_map_root_wads and _is_map_wad_path(bin_source_wad_key):
            continue
        old_file = old_files.get(bin_source_wad_key)
        if old_file is not None:
            old_targets.setdefault(bin_source_wad_key, old_file)
        new_file = new_files.get(bin_source_wad_key)
        if new_file is not None:
            new_targets.setdefault(bin_source_wad_key, new_file)

    return old_targets, new_targets


def _has_pending_sections(
    file_entry: WADFileDiffEntry,
    section_status_filter: set[ManifestDiffStatus],
) -> bool:
    """判断当前 WAD 是否存在待回填的 section."""
    return any(
        section.status in section_status_filter and section.path is None
        for section in file_entry.section_diffs
    )


def _resolve_root_wad_download_layout(
    *,
    root_wad_download_dir: str | PathLike[str] | None,
    cleanup_downloaded_root_wads: bool,
) -> tuple[Path, Path | None]:
    """解析 root WAD 下载目录与清理策略."""
    run_id = uuid4().hex
    if root_wad_download_dir is None:
        root_dir = _DEFAULT_ROOT_WAD_CACHE_DIR / f"run-{run_id}"
        return root_dir, root_dir

    configured_root = Path(root_wad_download_dir)
    if cleanup_downloaded_root_wads:
        root_dir = configured_root / f"run-{run_id}"
        return root_dir, root_dir
    return configured_root, None


def _download_root_wads_for_manifest(
    *,
    manifest: PatcherManifest,
    targets: Mapping[str, PatcherFile],
    output_root: Path,
    concurrency_limit: int | None,
) -> dict[str, Path]:
    """将目标 root WAD 下载到本地目录并返回路径索引."""
    if not targets:
        return {}

    files_to_download = list(targets.values())
    original_manifest_path = manifest.path
    manifest.path = str(output_root)
    try:
        _run_coroutine_compat(
            manifest.download_files_concurrently(
                files_to_download,
                concurrency_limit=concurrency_limit,
                raise_on_error=True,
            )
        )
    finally:
        manifest.path = original_manifest_path

    resolved_paths: dict[str, Path] = {}
    for wad_key, wad_file in targets.items():
        local_path = output_root / wad_file.name
        if local_path.is_file():
            resolved_paths[wad_key] = local_path
    return resolved_paths


def _run_coroutine_compat(awaitable: Any) -> Any:
    """在同步上下文执行协程."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    raise RuntimeError("当前事件循环已在运行，无法在同步接口内执行 root WAD 下载。")


def _is_map_wad_path(wad_path: str) -> bool:
    """判断是否为地图类 WAD 路径."""
    normalized = wad_path.strip().replace("\\", "/").lower()
    return _MAP_WAD_PATH_SEGMENT in normalized


def _resolve_manifest_context(
    *,
    wad_report: WADHeaderDiffReport,
    old_manifest: ManifestLike | None,
    new_manifest: ManifestLike | None,
) -> tuple[PatcherManifest, PatcherManifest]:
    """解析路径回填所需的 Manifest 上下文."""
    if old_manifest is not None or new_manifest is not None:
        if old_manifest is None or new_manifest is None:
            raise ValueError("old_manifest/new_manifest 需同时提供。")
        return _ensure_manifest(old_manifest), _ensure_manifest(new_manifest)

    old_cached = getattr(wad_report.manifest_report, "_old_manifest_obj", None)
    new_cached = getattr(wad_report.manifest_report, "_new_manifest_obj", None)
    if isinstance(old_cached, PatcherManifest) and isinstance(new_cached, PatcherManifest):
        return old_cached, new_cached
    raise ValueError("wad_report 中没有可复用的 Manifest 上下文，请显式传入 old_manifest/new_manifest。")


def _ensure_manifest(manifest: ManifestLike) -> PatcherManifest:
    """将 manifest 输入规范化为 `PatcherManifest`."""
    if isinstance(manifest, PatcherManifest):
        return manifest
    if isinstance(manifest, (str, PathLike)):
        return PatcherManifest(file=manifest, path="")
    raise TypeError(f"manifest 必须是 PatcherManifest 或路径/URL，当前类型: {type(manifest)!r}")


def _build_manifest_file_index(manifest: PatcherManifest) -> dict[str, PatcherFile]:
    """构建 manifest 文件索引（key 为小写路径）."""
    return {path.lower(): file_item for path, file_item in manifest.files.items()}


def _resolve_bin_source_wad_path(wad_path: str) -> str:
    """将区域 WAD 映射为 BIN 提取来源 WAD（优先根包）."""
    normalized = wad_path.strip().replace("\\", "/")
    if not normalized:
        return normalized

    lowered = normalized.lower()
    suffix = ".wad.client"
    if not lowered.endswith(suffix):
        return lowered

    stem = normalized[: -len(suffix)]
    if "." not in stem:
        return lowered

    base, last_segment = stem.rsplit(".", 1)
    if _LOCALE_SEGMENT_PATTERN.fullmatch(last_segment) is not None:
        return f"{base}{suffix}".lower()
    return lowered


def _normalize_section_statuses(
    statuses: Iterable[ManifestDiffStatus] | None,
) -> set[ManifestDiffStatus]:
    """规范化 section 状态过滤集合."""
    allowed: set[ManifestDiffStatus] = {"added", "removed", "changed", "unchanged"}
    default_statuses: set[ManifestDiffStatus] = {"added", "removed", "changed"}
    if statuses is None:
        return default_statuses
    normalized = {status for status in statuses if status in allowed}
    return normalized or default_statuses


def _normalize_paths(paths: Iterable[str]) -> tuple[str, ...]:
    """路径去重与标准化（反斜杠转正斜杠）."""
    deduplicated: dict[str, str] = {}
    for path in paths:
        if not isinstance(path, str):
            continue
        cleaned = path.strip().replace("\\", "/")
        if not cleaned:
            continue
        deduplicated.setdefault(cleaned.lower(), cleaned)
    return tuple(deduplicated[key] for key in sorted(deduplicated))
