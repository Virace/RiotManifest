"""WAD BIN 种子路径来源实现."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from os import PathLike
from typing import Protocol, runtime_checkable

from riotmanifest.manifest import PatcherManifest

ManifestLike = PatcherManifest | str | PathLike[str]

_CHAMPION_LANG_WAD_PATTERN = re.compile(
    r"/champions/(?P<name>[^/]+?)\.[a-z]{2}_[a-z]{2}\.wad\.client$",
    re.IGNORECASE,
)
_CHAMPION_WAD_PATTERN = re.compile(
    r"/champions/(?P<name>[^/]+?)\.wad\.client$",
    re.IGNORECASE,
)
_MAP_WAD_PATTERN = re.compile(
    r"/maps/shipping/(?P<map_dir>[^/]+)/(?P<file_name>[^/]+?)\.wad\.client$",
    re.IGNORECASE,
)


@runtime_checkable
class WADPathProvider(Protocol):
    """WAD BIN 种子路径来源协议."""

    def collect_paths(self, wad_path: str) -> tuple[str, ...]:
        """返回指定 WAD 的候选路径集合."""


class ManifestBinPathProvider:
    """基于命名规则提供 BIN 种子路径（不直接提供最终资源路径）."""

    def __init__(
        self,
        manifest: ManifestLike | None = None,
        wad_bin_paths: Mapping[str, Iterable[str]] | None = None,
        *,
        max_skin_id: int = 100,
        include_champion_root_bins: bool = True,
        include_map_bins: bool = True,
        global_paths: Iterable[str] = (),
    ):
        """初始化候选路径来源.

        Args:
            manifest: 兼容保留参数；当前实现无需使用。
            wad_bin_paths: 按 WAD 指定额外候选路径。
                key 为目标 WAD（忽略大小写）或 `"*"`。
            max_skin_id: 英雄皮肤 `skinN.bin` 的最大 N。
            include_champion_root_bins: 是否包含英雄通用 bin（`root.bin`、`{champion}.bin`）。
            include_map_bins: 是否包含地图通用 bin（`common.bin`、`mapXX.bin`）。
            global_paths: 对所有 WAD 生效的额外候选路径。

        Raises:
            ValueError: 参数值非法时抛出。
        """
        if max_skin_id < 0:
            raise ValueError("max_skin_id 不能小于 0。")

        self._manifest_ref = manifest
        self.max_skin_id = max_skin_id
        self.include_champion_root_bins = include_champion_root_bins
        self.include_map_bins = include_map_bins

        self._global_paths = _normalize_string_sequence(global_paths)
        self._extra_paths = _normalize_extra_paths(wad_bin_paths)
        self._cache: dict[str, tuple[str, ...]] = {}

    def __enter__(self) -> ManifestBinPathProvider:
        """进入上下文并返回当前实例."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """退出上下文。."""
        self.close()

    def close(self) -> None:
        """释放资源（当前实现为 no-op）。."""
        self._cache.clear()

    def collect_paths(self, wad_path: str) -> tuple[str, ...]:
        """返回目标 WAD 的候选路径集合."""
        target_key = wad_path.lower()
        cached = self._cache.get(target_key)
        if cached is not None:
            return cached

        candidates: set[str] = set(self._global_paths)
        candidates.update(self._extra_paths.get("*", tuple()))
        candidates.update(self._extra_paths.get(target_key, tuple()))
        candidates.update(self._build_champion_paths(target_key))
        candidates.update(self._build_map_paths(target_key))

        result = tuple(sorted(candidates))
        self._cache[target_key] = result
        return result

    def _build_champion_paths(self, wad_path: str) -> tuple[str, ...]:
        champion_name = _extract_champion_name(wad_path)
        if champion_name is None:
            return tuple()

        paths = [f"data/characters/{champion_name}/skins/skin{index}.bin" for index in range(self.max_skin_id + 1)]
        if self.include_champion_root_bins:
            paths.extend(
                (
                    f"data/characters/{champion_name}/skins/root.bin",
                    f"data/characters/{champion_name}/{champion_name}.bin",
                )
            )
        return tuple(paths)

    def _build_map_paths(self, wad_path: str) -> tuple[str, ...]:
        if not self.include_map_bins:
            return tuple()
        map_parts = _extract_map_parts(wad_path)
        if map_parts is None:
            return tuple()
        map_dir, wad_file_name = map_parts
        candidates = {
            "data/maps/shipping/common/common.bin",
            f"data/maps/shipping/{map_dir}/{map_dir}.bin",
        }
        if wad_file_name != map_dir:
            candidates.add(f"data/maps/shipping/{map_dir}/{wad_file_name}.bin")
        return tuple(sorted(candidates))


def _normalize_extra_paths(
    wad_bin_paths: Mapping[str, Iterable[str]] | None,
) -> dict[str, tuple[str, ...]]:
    if wad_bin_paths is None:
        return {}
    if not isinstance(wad_bin_paths, Mapping):
        raise TypeError("wad_bin_paths 必须是映射类型。")

    normalized: dict[str, tuple[str, ...]] = {}
    for wad_path, paths in wad_bin_paths.items():
        if not isinstance(wad_path, str):
            continue
        key = wad_path.strip().lower()
        if not key:
            continue
        values = _normalize_string_sequence(paths)
        if values:
            normalized[key] = values
    return normalized


def _normalize_string_sequence(values: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        cleaned = value.strip().replace("\\", "/").lower()
        if not cleaned:
            continue
        if cleaned in seen:
            continue
        seen.add(cleaned)
        normalized.append(cleaned)
    return tuple(normalized)


def _extract_champion_name(wad_path: str) -> str | None:
    match = _CHAMPION_LANG_WAD_PATTERN.search(wad_path)
    if match is None:
        match = _CHAMPION_WAD_PATTERN.search(wad_path)
    if match is None:
        return None
    name = match.group("name").strip().lower()
    return name or None


def _extract_map_parts(wad_path: str) -> tuple[str, str] | None:
    match = _MAP_WAD_PATTERN.search(wad_path)
    if match is None:
        return None
    map_dir = match.group("map_dir").strip().lower()
    file_name = match.group("file_name").strip().lower()
    if not map_dir or not file_name:
        return None
    return map_dir, file_name
