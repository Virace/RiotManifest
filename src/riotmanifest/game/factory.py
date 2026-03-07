"""LCU/GAME 元数据加载与提取器构造."""

from __future__ import annotations

import asyncio
import plistlib
import re
import threading
import warnings
from dataclasses import dataclass
from enum import Enum
from os import PathLike
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from loguru import logger

from riotmanifest.extractor import WADExtractor
from riotmanifest.game.metadata import (
    GAME_URL_TEMPLATE,
    LCU_URL,
    extract_manifest_id,
    fetch_game_data,
    fetch_lcu_data,
    version_key,
)
from riotmanifest.manifest import PatcherManifest

StrPath = str | PathLike[str]
WINDOWS_VERSION_PATTERN = re.compile(rb"(?:[0-9]\x00)+(?:\.\x00(?:[0-9]\x00)+){3}")
SYSTEM_BRANCH_PATTERN = re.compile(r"^\s*branch:\s*Releases/(?P<patch>\d+\.\d+)\s*$", re.MULTILINE)


class VersionMatchMode(str, Enum):  # noqa: UP042
    """版本匹配模式."""

    STRICT = "strict"
    IGNORE_REVISION = "ignore_revision"
    PATCH_LATEST = "patch_latest"


class VersionDisplayMode(str, Enum):  # noqa: UP042
    """统一版本号的显示模式."""

    IGNORE_REVISION = "ignore_revision"
    LCU = "lcu"
    GAME = "game"


@dataclass(frozen=True)
class VersionInfo:
    """版本信息标准模型."""

    normalized_build: str
    patch_version: str
    metadata_version: str | None = None
    exe_version: str | None = None

    @property
    def compact_version(self) -> str:
        """返回三段紧凑版本号."""
        return self.metadata_version or self.normalized_build

    @property
    def dotted_version(self) -> str:
        """返回四段点分版本号."""
        return self.exe_version or _compact_to_dotted_version(self.normalized_build)

    @property
    def display_version(self) -> str:
        """返回兼容旧接口的默认展示版本号."""
        if self.metadata_version is not None:
            return self.metadata_version
        return self.dotted_version


@dataclass(frozen=True)
class ManifestRef:
    """Manifest 引用信息."""

    artifact_group: str
    region: str
    source: str
    url: str
    manifest_id: str
    version: VersionInfo | None


@dataclass(frozen=True)
class ResolvedVersion:
    """统一版本号对象，可按不同模式输出字符串."""

    lcu: VersionInfo
    game: VersionInfo
    display_mode: VersionDisplayMode = VersionDisplayMode.IGNORE_REVISION

    @property
    def patch_version(self) -> str:
        """返回统一补丁版本."""
        return self.lcu.patch_version

    @property
    def value(self) -> str:
        """返回当前显示模式下的字符串值."""
        if self.display_mode is VersionDisplayMode.LCU:
            return self.lcu.dotted_version
        if self.display_mode is VersionDisplayMode.GAME:
            return self.game.compact_version
        return self.patch_version

    def with_display_mode(self, display_mode: VersionDisplayMode) -> ResolvedVersion:
        """返回切换显示模式后的新对象."""
        return ResolvedVersion(
            lcu=self.lcu,
            game=self.game,
            display_mode=display_mode,
        )

    def __str__(self) -> str:
        """返回当前显示模式下的统一版本号."""
        return self.value


@dataclass(frozen=True)
class ResolvedManifestPair:
    """按用户可见大区解析并匹配得到的一对 LCU/GAME manifest."""

    region: str
    version: ResolvedVersion
    lcu: ManifestRef
    game: ManifestRef
    match_mode: VersionMatchMode
    is_exact_match: bool
    match_reason: str
    candidate_count: int


# Compatibility alias; remove in v3.0.0.
LiveManifestPair = ResolvedManifestPair


@dataclass(frozen=True)
class _RegionConfigRecord:
    """单个用户可见大区对应的底层配置记录."""

    canonical_region: str
    patchline: str
    lcu_config_id: str
    launcher_region: str | None
    manifest_url: str
    manifest_id: str
    version_hint: str | None
    game_version_set: str | None
    game_artifact_type: str | None
    game_platform: str | None
    aliases: tuple[str, ...]


# Compatibility alias; remove in v3.0.0.
_LcuConfigRecord = _RegionConfigRecord


class LeagueManifestError(Exception):
    """League manifest 解析相关错误基类."""


# Compatibility alias; remove in v3.0.0.
RiotGameDataError = LeagueManifestError


class RegionConfigNotFoundError(LeagueManifestError):
    """目标大区不存在对应客户端配置时抛出."""


# Compatibility alias; remove in v3.0.0.
PatchlineConfigNotFoundError = RegionConfigNotFoundError
# Compatibility alias; remove in v3.0.0.
LiveConfigNotFoundError = RegionConfigNotFoundError


class LcuVersionUnavailableError(LeagueManifestError):
    """无法严格解析 LCU 版本时抛出."""


class ConsistentGameManifestNotFoundError(LeagueManifestError):
    """无法找到满足匹配规则的 GAME manifest 时抛出."""


DEPRECATED_LATEST_API_REMOVE_VERSION = "3.0.0"
DEPRECATED_RIOT_GAME_DATA_ALIAS_REMOVE_VERSION = "3.0.0"


def _warn_deprecated_latest_api(*, owner_name: str, api_name: str, replacement: str) -> None:
    """发出 latest 兼容接口的弃用提示.

    Args:
        owner_name: 触发该接口的类名。
        api_name: 被调用的旧接口名。
        replacement: 推荐替代调用说明。
    """
    warnings.warn(
        (
            f"{owner_name}.{api_name}() 已弃用，计划在 v{DEPRECATED_LATEST_API_REMOVE_VERSION} 删除。"
            f"请改用 {replacement}。"
        ),
        FutureWarning,
        stacklevel=3,
    )


def _warn_deprecated_riot_game_data_alias() -> None:
    """发出 `RiotGameData` 旧类名的弃用提示."""
    warnings.warn(
        (
            "RiotGameData 已弃用，计划在 "
            f"v{DEPRECATED_RIOT_GAME_DATA_ALIAS_REMOVE_VERSION} 删除。"
            "请改用 LeagueManifestResolver。"
        ),
        FutureWarning,
        stacklevel=3,
    )


def _run_coroutine_sync(coroutine: Any) -> Any:
    """在同步上下文中执行协程，即使当前线程已存在事件循环."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coroutine)
        except BaseException as exc:  # noqa: BLE001
            error["value"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if "value" in error:
        raise error["value"]
    return result.get("value")




def _compact_to_dotted_version(compact_version: str) -> str:
    """把三段紧凑版本号转换为四段点分版本号."""
    parts = compact_version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValueError(f"无法把紧凑版本号转换为四段格式: {compact_version}")

    build = parts[2]
    if len(build) <= 4:
        raise ValueError(f"紧凑版本号第三段长度不足，无法拆分为 3/4 结构: {compact_version}")
    return f"{parts[0]}.{parts[1]}.{build[:-4]}.{build[-4:]}"


def _normalize_metadata_version(metadata_version: str) -> tuple[str, str]:
    """标准化 metadata 版本号并返回紧凑 build 与补丁号."""
    sanitized = metadata_version.split("+", 1)[0]
    parts = sanitized.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ConsistentGameManifestNotFoundError(f"无法解析 GAME metadata 版本号: {metadata_version}")
    if len(parts[2]) <= 4:
        raise ConsistentGameManifestNotFoundError(
            f"GAME metadata 版本号不满足 3/4 结构约束: {metadata_version}"
        )
    return sanitized, f"{parts[0]}.{parts[1]}"


def _normalize_exe_version(exe_version: str) -> tuple[str, str]:
    """标准化 exe 版本号并返回紧凑 build 与补丁号."""
    parts = exe_version.split(".")
    if len(parts) != 4 or not all(part.isdigit() for part in parts):
        raise LcuVersionUnavailableError(f"无法解析 exe 版本号: {exe_version}")
    if len(parts[3]) != 4:
        raise LcuVersionUnavailableError(f"exe 版本号第四段不满足 4 位约束: {exe_version}")
    return f"{parts[0]}.{parts[1]}.{parts[2]}{parts[3]}", f"{parts[0]}.{parts[1]}"

def _build_lcu_version_info(exe_version: str) -> VersionInfo:
    """把 LCU exe 版本转换为标准版本模型."""
    compact_version, patch_version = _normalize_exe_version(exe_version)
    return VersionInfo(
        normalized_build=compact_version,
        patch_version=patch_version,
        exe_version=exe_version,
    )


def _build_game_version_info(metadata_version: str) -> VersionInfo:
    """把 GAME metadata 版本转换为标准版本模型."""
    compact_version, patch_version = _normalize_metadata_version(metadata_version)
    return VersionInfo(
        normalized_build=compact_version,
        patch_version=patch_version,
        metadata_version=compact_version,
    )


def _is_not_newer_than_lcu(*, game_version: VersionInfo, lcu_version: VersionInfo) -> bool:
    """判断 GAME build 是否不高于给定 LCU build.

    Args:
        game_version: GAME 侧版本信息。
        lcu_version: LCU 侧版本信息。

    Returns:
        当 GAME build 小于等于 LCU build 时返回 `True`。
    """
    return version_key(game_version.normalized_build) <= version_key(lcu_version.normalized_build)


def _select_highest_game_candidate(candidates: list[ManifestRef]) -> ManifestRef:
    """从 GAME 候选中选出版本号最大的一个.

    Args:
        candidates: 已保证携带版本信息的 GAME 候选列表。

    Returns:
        版本号最大的 GAME 候选。
    """
    return max(
        candidates,
        key=lambda item: version_key(item.version.normalized_build if item.version else ""),
    )


class _LcuVersionResolver:
    """从 LCU manifest 中提取真实客户端版本."""

    def __init__(self) -> None:
        """初始化版本缓存."""
        self._cache: dict[str, VersionInfo] = {}

    def resolve(self, manifest_url: str) -> VersionInfo:
        """解析 manifest 对应的 LCU 精确版本."""
        cached = self._cache.get(manifest_url)
        if cached is not None:
            return cached

        with TemporaryDirectory(prefix="riotmanifest_lcu_") as temp_dir:
            manifest = PatcherManifest(file=manifest_url, path=temp_dir)
            version = self._resolve_from_manifest(manifest=manifest, temp_dir=Path(temp_dir))

        self._cache[manifest_url] = version
        return version

    def _resolve_from_manifest(self, manifest: PatcherManifest, temp_dir: Path) -> VersionInfo:
        """从 manifest 内容中选择合适的版本载体进行解析."""
        if "LeagueClient.exe" in manifest.files:
            payload = self._download_manifest_file(
                manifest=manifest,
                target_file=manifest.files["LeagueClient.exe"],
                temp_dir=temp_dir,
            )
            return _build_lcu_version_info(self._extract_windows_version(payload))

        plist_path = "Contents/LoL/LeagueClient.app/Contents/Info.plist"
        if plist_path in manifest.files:
            payload = self._download_manifest_file(
                manifest=manifest,
                target_file=manifest.files[plist_path],
                temp_dir=temp_dir,
            )
            return _build_lcu_version_info(self._extract_macos_version(payload))

        patch_version = self._extract_patch_version_hint(manifest=manifest, temp_dir=temp_dir)
        if patch_version is not None:
            raise LcuVersionUnavailableError(
                f"manifest {manifest.file} 只能解析到补丁版本 {patch_version}，无法得到精确 LCU build"
            )

        raise LcuVersionUnavailableError(f"manifest {manifest.file} 中不存在可用的 LCU 版本载体")

    @staticmethod
    def _download_manifest_file(
        *,
        manifest: PatcherManifest,
        target_file: Any,
        temp_dir: Path,
    ) -> bytes:
        """下载目标文件并返回字节内容."""
        results = _run_coroutine_sync(
            manifest.download_files_concurrently(
                [target_file],
                concurrency_limit=1,
                raise_on_error=True,
            )
        )
        if not results or not results[0]:
            raise LcuVersionUnavailableError(f"下载版本载体失败: {target_file.name}")
        return (temp_dir / target_file.name).read_bytes()

    @staticmethod
    def _extract_windows_version(payload: bytes) -> str:
        """从 Windows PE 文件中提取 `FileVersion` 或 `ProductVersion`."""
        for label in ("ProductVersion", "FileVersion"):
            marker = label.encode("utf-16le")
            index = payload.find(marker)
            if index < 0:
                continue

            window = payload[index : index + 512]
            matches = WINDOWS_VERSION_PATTERN.findall(window)
            for match in matches:
                version = match.decode("utf-16le", errors="ignore")
                if version.count(".") == 3:
                    return version

        raise LcuVersionUnavailableError("无法从 LeagueClient.exe 中提取 FileVersion/ProductVersion")

    @staticmethod
    def _extract_macos_version(payload: bytes) -> str:
        """从 macOS Info.plist 中提取版本.

        Notes:
            这是非主要支持路径，当前主要版本提取目标仍是 Windows 客户端载体。
        """
        plist_data = plistlib.loads(payload)
        for key in ("FileVersion", "CFBundleVersion", "CFBundleShortVersionString"):
            value = plist_data.get(key)
            if isinstance(value, str) and value:
                return value
        raise LcuVersionUnavailableError("无法从 Info.plist 中提取客户端版本")

    def _extract_patch_version_hint(self, *, manifest: PatcherManifest, temp_dir: Path) -> str | None:
        """从 `system.yaml` 中提取非严格补丁提示.

        Notes:
            该路径只提供弱提示，不参与严格版本解析，后续计划删除。
        """
        system_file = manifest.files.get("system.yaml")
        if system_file is None:
            return None

        payload = self._download_manifest_file(
            manifest=manifest,
            target_file=system_file,
            temp_dir=temp_dir,
        )
        content = payload.decode("utf-8", errors="ignore")
        match = SYSTEM_BRANCH_PATTERN.search(content)
        if match is None:
            return None
        return match.group("patch")


class LeagueManifestResolver:
    """按用户可见大区整合《英雄联盟》LCU/GAME 清单并构造一致版本对."""

    def __init__(self) -> None:
        """初始化大区映射缓存与版本解析器."""
        self._lcu_data: dict[str, _RegionConfigRecord] = {}
        self._region_aliases: dict[str, str] = {}
        self._game_data: dict[str, list[ManifestRef]] = {}
        self._lcu_version_resolver = _LcuVersionResolver()

    def _resolve_region_record(self, region: str) -> _RegionConfigRecord:
        """按用户输入的大区标识解析底层配置记录."""
        if not self._lcu_data:
            self.load_lcu_data()

        normalized_region = region.strip().upper()
        canonical_region = self._region_aliases.get(normalized_region, normalized_region)
        record = self._lcu_data.get(canonical_region)
        if record is None:
            raise RegionConfigNotFoundError(f"区域 {region} 没有可用的客户端配置")
        return record

    def load_lcu_data(self) -> None:
        """加载并解析所有 patchline 的 LCU 配置数据."""
        raw_data = fetch_lcu_data(url=LCU_URL)
        records: dict[str, _RegionConfigRecord] = {}
        aliases: dict[str, str] = {}

        for patchline_items in raw_data.values():
            for item in patchline_items.values():
                canonical_region = str(item.get("canonical_region") or "").strip().upper()
                if not canonical_region:
                    continue

                alias_values = tuple(
                    alias.strip().upper()
                    for alias in (item.get("region_aliases") or [])
                    if isinstance(alias, str) and alias.strip()
                ) or (canonical_region,)
                record = _RegionConfigRecord(
                    canonical_region=canonical_region,
                    patchline=str(item.get("patchline") or "").strip().lower(),
                    lcu_config_id=str(item.get("lcu_config_id") or "").strip().upper(),
                    launcher_region=(
                        str(item.get("launcher_region") or "").strip().upper() or None
                    ),
                    manifest_url=item["url"],
                    manifest_id=item["manifest_id"],
                    version_hint=item.get("version_hint") or None,
                    game_version_set=(
                        str(item.get("game_version_set") or "").strip().upper() or None
                    ),
                    game_artifact_type=item.get("game_artifact_type") or None,
                    game_platform=item.get("game_platform") or None,
                    aliases=alias_values,
                )
                records[canonical_region] = record
                for alias in alias_values:
                    existing_region = aliases.get(alias)
                    if existing_region is not None and existing_region != canonical_region:
                        logger.warning(
                            "区域别名 {} 同时命中 {} 与 {}，保留 {}",
                            alias,
                            existing_region,
                            canonical_region,
                            existing_region,
                        )
                        continue
                    aliases[alias] = canonical_region

        self._lcu_data = records
        self._region_aliases = aliases

    def load_game_data(self, regions: list[str] | None = None) -> None:
        """加载并解析指定大区的 GAME 候选数据."""
        regions = regions or ["EUW1", "PBE1"]
        logger.debug("正在加载 GAME 数据，大区={}", regions)

        for region in regions:
            normalized_region = region.strip().upper()
            record = None
            if self._lcu_data:
                try:
                    record = self._resolve_region_record(region)
                except RegionConfigNotFoundError:
                    record = None

            if record is None:
                canonical_region = normalized_region
                version_set = normalized_region
                artifact_type = "lol-game-client"
                artifact_platform = "windows"
            else:
                canonical_region = record.canonical_region
                version_set = record.game_version_set or canonical_region
                artifact_type = record.game_artifact_type or "lol-game-client"
                artifact_platform = record.game_platform or "windows"

            releases = fetch_game_data(
                region=version_set,
                url_template=GAME_URL_TEMPLATE,
                artifact_type=artifact_type,
                platform=artifact_platform,
            )
            self._game_data[canonical_region] = [
                ManifestRef(
                    artifact_group="game",
                    region=canonical_region,
                    source="sieve",
                    url=item["url"],
                    manifest_id=extract_manifest_id(item["url"]),
                    version=_build_game_version_info(item["version"]),
                )
                for item in releases
            ]

    def latest_lcu(self, region: str = "EUW") -> dict[str, str] | None:
        """获取指定大区当前 LCU 配置的兼容视图.

        Deprecated:
            该兼容接口计划在 `v3.0.0` 删除。请改用
            `resolve_manifest_pair(region, match_mode=VersionMatchMode.PATCH_LATEST)`
            或 `get_lcu_manifest(region)`。
        """
        _warn_deprecated_latest_api(
            owner_name=self.__class__.__name__,
            api_name="latest_lcu",
            replacement=(
                "resolve_manifest_pair(region, match_mode=VersionMatchMode.PATCH_LATEST)"
                " 或 get_lcu_manifest(region)"
            ),
        )
        try:
            record = self._resolve_region_record(region)
        except RegionConfigNotFoundError:
            return None
        return {
            "version": record.version_hint or "",
            "url": record.manifest_url,
        }

    def latest_game(self, region: str = "EUW") -> dict[str, str] | None:
        """获取指定大区下版本号最大的 GAME 候选.

        Deprecated:
            该兼容接口计划在 `v3.0.0` 删除。请改用
            `resolve_manifest_pair(region, match_mode=VersionMatchMode.PATCH_LATEST)`。
        """
        _warn_deprecated_latest_api(
            owner_name=self.__class__.__name__,
            api_name="latest_game",
            replacement="resolve_manifest_pair(region, match_mode=VersionMatchMode.PATCH_LATEST)",
        )
        resolved_region = region.strip().upper()
        releases = self._game_data.get(resolved_region)
        if releases is None:
            try:
                releases = self.list_game_candidates(region)
            except RegionConfigNotFoundError:
                if resolved_region not in self._game_data:
                    self.load_game_data(regions=[resolved_region])
                releases = self._game_data.get(resolved_region, [])
            else:
                resolved_region = self._resolve_region_record(region).canonical_region

        if not releases:
            return None
        latest = _select_highest_game_candidate(releases)
        if latest.version is None:
            return None
        return {
            "version": latest.version.display_version,
            "url": latest.url,
        }

    def get_lcu_manifest(self, region: str = "EUW") -> ManifestRef:
        """返回指定大区当前 LCU 配置中的 manifest 引用.

        Args:
            region: 用户可见的大区标识，例如 `EUW`、`EUW1` 或 `PBE`。

        Returns:
            当前大区对应的 LCU manifest 引用。

        Raises:
            RegionConfigNotFoundError: 当目标大区不存在对应配置时抛出。
        """
        record = self._resolve_region_record(region)

        return ManifestRef(
            artifact_group="lcu",
            region=record.canonical_region,
            source="clientconfig",
            url=record.manifest_url,
            manifest_id=record.manifest_id,
            version=None,
        )

    def list_game_candidates(self, region: str = "EUW") -> list[ManifestRef]:
        """列出指定大区对应的 GAME 候选集合.

        Args:
            region: 用户可见的大区标识，例如 `EUW`、`EUW1` 或 `PBE`。

        Returns:
            该大区关联的 GAME manifest 候选列表。

        Raises:
            RegionConfigNotFoundError: 当目标大区不存在对应配置时抛出。
        """
        record = self._resolve_region_record(region)
        if not record.game_version_set:
            raise RegionConfigNotFoundError(f"区域 {region} 缺少可用的 GAME version-set")

        if record.canonical_region not in self._game_data:
            releases = fetch_game_data(
                region=record.game_version_set,
                url_template=GAME_URL_TEMPLATE,
                artifact_type=record.game_artifact_type or "lol-game-client",
                platform=record.game_platform or "windows",
            )
            self._game_data[record.canonical_region] = [
                ManifestRef(
                    artifact_group="game",
                    region=record.canonical_region,
                    source="sieve",
                    url=item["url"],
                    manifest_id=extract_manifest_id(item["url"]),
                    version=_build_game_version_info(item["version"]),
                )
                for item in releases
            ]

        return list(self._game_data[record.canonical_region])

    def resolve_manifest_pair(
        self,
        region: str = "EUW",
        *,
        match_mode: VersionMatchMode = VersionMatchMode.IGNORE_REVISION,
        version_display_mode: VersionDisplayMode = VersionDisplayMode.IGNORE_REVISION,
    ) -> ResolvedManifestPair:
        """解析指定大区当前版本规则明确的一对 LCU/GAME manifest.

        Args:
            region: 用户可见的大区标识，例如 `EUW`、`EUW1` 或 `PBE`。
            match_mode: 版本匹配模式。`strict` 需要 build 完全一致；
                `ignore_revision` 允许只按 `major.minor` 匹配。
            version_display_mode: 统一版本号的默认显示模式。

        Returns:
            指定大区解析出的 manifest 对。

        Raises:
            RegionConfigNotFoundError: 当目标大区不存在有效配置时抛出。
            LcuVersionUnavailableError: 当无法解析 LCU 精确版本时抛出。
            ConsistentGameManifestNotFoundError: 当找不到满足规则的 GAME 清单时抛出。
        """
        if isinstance(match_mode, str):
            match_mode = VersionMatchMode(match_mode)
        if isinstance(version_display_mode, str):
            version_display_mode = VersionDisplayMode(version_display_mode)

        record = self._resolve_region_record(region)
        resolved_region = record.canonical_region
        lcu_manifest = self.get_lcu_manifest(region)
        lcu_version = self._lcu_version_resolver.resolve(lcu_manifest.url)
        lcu_manifest = ManifestRef(
            artifact_group=lcu_manifest.artifact_group,
            region=lcu_manifest.region,
            source=lcu_manifest.source,
            url=lcu_manifest.url,
            manifest_id=lcu_manifest.manifest_id,
            version=lcu_version,
        )

        candidates = self.list_game_candidates(region)
        exact_matches = [
            candidate
            for candidate in candidates
            if candidate.version and candidate.version.normalized_build == lcu_version.normalized_build
        ]
        if exact_matches:
            selected = max(
                exact_matches,
                key=lambda item: version_key(item.version.display_version if item.version else ""),
            )
            return ResolvedManifestPair(
                region=resolved_region,
                version=ResolvedVersion(
                    lcu=lcu_version,
                    game=selected.version,
                    display_mode=version_display_mode,
                ),
                lcu=lcu_manifest,
                game=selected,
                match_mode=match_mode,
                is_exact_match=True,
                match_reason="normalized_build_match",
                candidate_count=len(candidates),
            )

        if match_mode is VersionMatchMode.STRICT:
            raise ConsistentGameManifestNotFoundError(
                f"区域 {resolved_region} 未找到与 LCU build "
                f"{lcu_version.normalized_build} 完全一致的 GAME manifest"
            )

        patch_matches = [
            candidate
            for candidate in candidates
            if candidate.version and candidate.version.patch_version == lcu_version.patch_version
        ]
        if not patch_matches:
            raise ConsistentGameManifestNotFoundError(
                f"区域 {resolved_region} 未找到与补丁版本 {lcu_version.patch_version} 一致的 GAME manifest"
            )

        if match_mode is VersionMatchMode.PATCH_LATEST:
            selected = _select_highest_game_candidate(patch_matches)
            return ResolvedManifestPair(
                region=resolved_region,
                version=ResolvedVersion(
                    lcu=lcu_version,
                    game=selected.version,
                    display_mode=version_display_mode,
                ),
                lcu=lcu_manifest,
                game=selected,
                match_mode=match_mode,
                is_exact_match=bool(
                    selected.version and selected.version.normalized_build == lcu_version.normalized_build
                ),
                match_reason="patch_latest_fallback",
                candidate_count=len(candidates),
            )

        # live 实测表明安装器不会优先选择比当前 LCU 更高的 GAME build，
        # 因此默认宽松模式也必须把候选限制在“同补丁且不高于 LCU”这一安全子集内。
        compatible_patch_matches = [
            candidate
            for candidate in patch_matches
            if candidate.version
            and _is_not_newer_than_lcu(
                game_version=candidate.version,
                lcu_version=lcu_version,
            )
        ]
        if not compatible_patch_matches:
            raise ConsistentGameManifestNotFoundError(
                f"区域 {resolved_region} 在补丁 {lcu_version.patch_version} 下没有不高于 "
                f"LCU build {lcu_version.normalized_build} 的 GAME manifest"
            )

        selected = _select_highest_game_candidate(compatible_patch_matches)
        return ResolvedManifestPair(
            region=resolved_region,
            version=ResolvedVersion(
                lcu=lcu_version,
                game=selected.version,
                display_mode=version_display_mode,
            ),
            lcu=lcu_manifest,
            game=selected,
            match_mode=match_mode,
            is_exact_match=bool(
                selected.version and selected.version.normalized_build == lcu_version.normalized_build
            ),
            match_reason="ignore_revision_fallback",
            candidate_count=len(candidates),
        )

    def resolve_version(
        self,
        region: str = "EUW",
        *,
        match_mode: VersionMatchMode = VersionMatchMode.IGNORE_REVISION,
        display_mode: VersionDisplayMode = VersionDisplayMode.IGNORE_REVISION,
    ) -> ResolvedVersion:
        """返回指定大区的统一版本号对象."""
        pair = self.resolve_manifest_pair(
            region=region,
            match_mode=match_mode,
            version_display_mode=display_mode,
        )
        return pair.version

    def build_lcu_extractor(
        self,
        region: str = "EUW",
        *,
        manifest_path: StrPath = "",
        **extractor_kwargs: Any,
    ) -> WADExtractor:
        """为指定大区的 LCU 配置构造 WADExtractor."""
        lcu_manifest = self.get_lcu_manifest(region)
        manifest = PatcherManifest(file=lcu_manifest.url, path=manifest_path)
        return WADExtractor(manifest, **extractor_kwargs)

    def build_game_extractor(
        self,
        region: str = "EUW",
        *,
        manifest_path: StrPath = "",
        match_mode: VersionMatchMode = VersionMatchMode.IGNORE_REVISION,
        **extractor_kwargs: Any,
    ) -> WADExtractor:
        """为指定大区的一致 GAME manifest 构造 WADExtractor."""
        pair = self.resolve_manifest_pair(region, match_mode=match_mode)
        manifest = PatcherManifest(file=pair.game.url, path=manifest_path)
        return WADExtractor(manifest, **extractor_kwargs)

    def available_regions(self) -> list[str]:
        """返回当前可用的用户可见大区列表."""
        if not self._lcu_data:
            self.load_lcu_data()
        return sorted(self._lcu_data.keys())

    def available_lcu_regions(self) -> list[str]:
        """兼容旧方法名，计划在 `v3.0.0` 删除."""
        return self.available_regions()

    def available_game_regions(self) -> list[str]:
        """返回当前缓存中的 GAME 候选大区列表."""
        return sorted(self._game_data.keys())


class RiotGameData(LeagueManifestResolver):
    """`LeagueManifestResolver` 的兼容旧名，计划在 `v3.0.0` 删除."""

    def __init__(self) -> None:
        """初始化兼容旧名实例，并发出弃用提示."""
        _warn_deprecated_riot_game_data_alias()
        super().__init__()
