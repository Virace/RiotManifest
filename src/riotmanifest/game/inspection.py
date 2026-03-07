"""任意英雄联盟 manifest 的类型识别与版本提取."""

from __future__ import annotations

import json
from os import PathLike, fspath
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse

from riotmanifest.game.factory import (
    ConsistentGameManifestNotFoundError,
    LcuVersionUnavailableError,
    LeagueManifestError,
    ManifestRef,
    ResolvedManifestPair,
    ResolvedVersion,
    VersionDisplayMode,
    VersionInfo,
    VersionMatchMode,
    _build_game_version_info,
    _build_lcu_version_info,
    _is_not_newer_than_lcu,
    _LcuVersionResolver,
)
from riotmanifest.manifest import PatcherManifest

StrPath = str | PathLike[str]

INSPECTION_REGION = "inspection"
INSPECTION_SOURCE = "manifest_inspector"
GAME_CONTENT_METADATA_PATH = "content-metadata.json"
GAME_EXE_PATH = "League of Legends.exe"
LCU_EXE_PATH = "LeagueClient.exe"
LCU_MACOS_INFO_PLIST_PATH = "Contents/LoL/LeagueClient.app/Contents/Info.plist"


class ManifestInspectionError(LeagueManifestError):
    """任意 manifest 探测过程中的错误."""


class LeagueManifestInspector:
    """从单个或两个 manifest 输入中提取类型与版本信息."""

    def __init__(self) -> None:
        """初始化 Inspector 依赖."""
        self._lcu_version_resolver = _LcuVersionResolver()

    def inspect_manifests(
        self,
        *sources: StrPath,
        match_mode: VersionMatchMode = VersionMatchMode.IGNORE_REVISION,
        version_display_mode: VersionDisplayMode = VersionDisplayMode.IGNORE_REVISION,
    ) -> ManifestRef | ResolvedManifestPair:
        """按输入数量自动探测单个或两个 manifest.

        Args:
            *sources: 1 个或 2 个 manifest 本地路径 / URL。
            match_mode: 当输入 2 个 manifest 时使用的版本匹配模式。
            version_display_mode: 当输入 2 个 manifest 时使用的显示模式。

        Returns:
            单个输入时返回该 manifest 的识别结果；两个输入时返回配对结果。

        Raises:
            ValueError: 输入数量不是 1 或 2 时抛出。
        """
        if len(sources) == 1:
            return self.inspect_manifest(sources[0])
        if len(sources) == 2:
            return self.inspect_pair(
                sources[0],
                sources[1],
                match_mode=match_mode,
                version_display_mode=version_display_mode,
            )
        raise ValueError("inspect_manifests 只支持 1 个或 2 个 manifest 输入")

    def inspect_manifest(self, source: StrPath) -> ManifestRef:
        """识别单个 manifest 的类型与版本.

        Args:
            source: manifest 本地路径或远程 URL。

        Returns:
            携带类型与版本信息的 manifest 引用。
        """
        source_ref = fspath(source)
        with TemporaryDirectory(prefix="riotmanifest_inspect_") as temp_dir_str:
            temp_dir = Path(temp_dir_str)
            manifest = PatcherManifest(file=source_ref, path=temp_dir_str)
            artifact_group = self._detect_artifact_group(manifest)
            version = self._resolve_version(
                manifest=manifest,
                artifact_group=artifact_group,
                temp_dir=temp_dir,
            )

        return ManifestRef(
            artifact_group=artifact_group,
            region=INSPECTION_REGION,
            source=INSPECTION_SOURCE,
            url=source_ref,
            manifest_id=_extract_manifest_id_from_source(source_ref),
            version=version,
        )

    def inspect_pair(
        self,
        first: StrPath,
        second: StrPath,
        *,
        match_mode: VersionMatchMode = VersionMatchMode.IGNORE_REVISION,
        version_display_mode: VersionDisplayMode = VersionDisplayMode.IGNORE_REVISION,
    ) -> ResolvedManifestPair:
        """探测两个 manifest 并在可配对时返回结果.

        Args:
            first: 第一个 manifest 本地路径或远程 URL。
            second: 第二个 manifest 本地路径或远程 URL。
            match_mode: 版本匹配模式。
            version_display_mode: 统一版本显示模式。

        Returns:
            满足配对规则的一对 manifest 结果。

        Raises:
            ManifestInspectionError: 输入不构成一对 LCU/GAME 时抛出。
            ConsistentGameManifestNotFoundError: 两个 manifest 版本不满足匹配规则时抛出。
        """
        if isinstance(match_mode, str):
            match_mode = VersionMatchMode(match_mode)
        if isinstance(version_display_mode, str):
            version_display_mode = VersionDisplayMode(version_display_mode)

        inspected = [self.inspect_manifest(first), self.inspect_manifest(second)]
        lcu_manifest, game_manifest = self._split_pair(inspected)
        lcu_version = _require_manifest_version(lcu_manifest)
        game_version = _require_manifest_version(game_manifest)

        is_exact_match = game_version.normalized_build == lcu_version.normalized_build
        if is_exact_match:
            return _build_live_manifest_pair(
                lcu=lcu_manifest,
                game=game_manifest,
                match_mode=match_mode,
                version_display_mode=version_display_mode,
                is_exact_match=True,
                match_reason="normalized_build_match",
            )

        if match_mode is VersionMatchMode.STRICT:
            raise ConsistentGameManifestNotFoundError(
                "给定 manifest 未满足严格匹配："
                f"LCU={lcu_version.normalized_build}, GAME={game_version.normalized_build}"
            )

        if game_version.patch_version != lcu_version.patch_version:
            raise ConsistentGameManifestNotFoundError(
                "给定 manifest 补丁版本不一致："
                f"LCU={lcu_version.patch_version}, GAME={game_version.patch_version}"
            )

        if match_mode is VersionMatchMode.PATCH_LATEST:
            return _build_live_manifest_pair(
                lcu=lcu_manifest,
                game=game_manifest,
                match_mode=match_mode,
                version_display_mode=version_display_mode,
                is_exact_match=False,
                match_reason="patch_latest_fallback",
            )

        if not _is_not_newer_than_lcu(
            game_version=game_version,
            lcu_version=lcu_version,
        ):
            raise ConsistentGameManifestNotFoundError(
                "给定 manifest 在同补丁下不满足 ignore_revision 规则："
                f"GAME build {game_version.normalized_build} 高于 "
                f"LCU build {lcu_version.normalized_build}"
            )

        return _build_live_manifest_pair(
            lcu=lcu_manifest,
            game=game_manifest,
            match_mode=match_mode,
            version_display_mode=version_display_mode,
            is_exact_match=False,
            match_reason="ignore_revision_fallback",
        )

    @staticmethod
    def _detect_artifact_group(manifest: PatcherManifest) -> str:
        """根据强特征文件判断 manifest 类型."""
        has_game_markers = any(
            path in manifest.files
            for path in (
                GAME_CONTENT_METADATA_PATH,
                GAME_EXE_PATH,
            )
        )
        has_lcu_markers = any(
            path in manifest.files
            for path in (
                LCU_EXE_PATH,
                LCU_MACOS_INFO_PLIST_PATH,
            )
        )
        if has_game_markers and has_lcu_markers:
            raise ManifestInspectionError("manifest 同时命中 LCU/GAME 特征，无法判定类型")
        if has_game_markers:
            return "game"
        if has_lcu_markers:
            return "lcu"
        return "unknown"

    def _resolve_version(
        self,
        *,
        manifest: PatcherManifest,
        artifact_group: str,
        temp_dir: Path,
    ) -> VersionInfo | None:
        """按识别出的类型提取版本."""
        if artifact_group == "game":
            return self._resolve_game_version(manifest=manifest, temp_dir=temp_dir)
        if artifact_group == "lcu":
            return self._resolve_lcu_version(manifest=manifest, temp_dir=temp_dir)
        return None

    def _resolve_game_version(
        self,
        *,
        manifest: PatcherManifest,
        temp_dir: Path,
    ) -> VersionInfo:
        """提取 GAME manifest 的版本信息."""
        if GAME_CONTENT_METADATA_PATH in manifest.files:
            payload = _download_manifest_payload(
                manifest=manifest,
                file_path=GAME_CONTENT_METADATA_PATH,
                temp_dir=temp_dir,
            )
            version = _extract_game_version_from_metadata(payload)
            return _build_game_version_info(version)

        payload = _download_manifest_payload(
            manifest=manifest,
            file_path=GAME_EXE_PATH,
            temp_dir=temp_dir,
        )
        exe_version = self._lcu_version_resolver._extract_windows_version(payload)
        return _build_lcu_version_info(exe_version)

    def _resolve_lcu_version(
        self,
        *,
        manifest: PatcherManifest,
        temp_dir: Path,
    ) -> VersionInfo:
        """提取 LCU manifest 的版本信息."""
        if LCU_EXE_PATH in manifest.files:
            payload = _download_manifest_payload(
                manifest=manifest,
                file_path=LCU_EXE_PATH,
                temp_dir=temp_dir,
            )
            exe_version = self._lcu_version_resolver._extract_windows_version(payload)
            return _build_lcu_version_info(exe_version)

        if LCU_MACOS_INFO_PLIST_PATH in manifest.files:
            payload = _download_manifest_payload(
                manifest=manifest,
                file_path=LCU_MACOS_INFO_PLIST_PATH,
                temp_dir=temp_dir,
            )
            exe_version = self._lcu_version_resolver._extract_macos_version(payload)
            return _build_lcu_version_info(exe_version)

        raise LcuVersionUnavailableError(f"manifest {manifest.file} 中不存在可用的 LCU 版本载体")

    @staticmethod
    def _split_pair(inspected: list[ManifestRef]) -> tuple[ManifestRef, ManifestRef]:
        """从两个识别结果中拆出一对 LCU/GAME manifest."""
        if len(inspected) != 2:
            raise ManifestInspectionError("仅支持从两个 manifest 结果中拆分配对")

        lcu_manifests = [item for item in inspected if item.artifact_group == "lcu"]
        game_manifests = [item for item in inspected if item.artifact_group == "game"]
        if len(lcu_manifests) != 1 or len(game_manifests) != 1:
            raise ManifestInspectionError(
                "给定输入无法组成一对 LCU/GAME manifest："
                f"lcu={len(lcu_manifests)}, game={len(game_manifests)}"
            )
        return lcu_manifests[0], game_manifests[0]


def _build_live_manifest_pair(
    *,
    lcu: ManifestRef,
    game: ManifestRef,
    match_mode: VersionMatchMode,
    version_display_mode: VersionDisplayMode,
    is_exact_match: bool,
    match_reason: str,
) -> ResolvedManifestPair:
    """把 Inspector 识别结果组装成 `ResolvedManifestPair`."""
    return ResolvedManifestPair(
        region=INSPECTION_REGION,
        version=ResolvedVersion(
            lcu=_require_manifest_version(lcu),
            game=_require_manifest_version(game),
            display_mode=version_display_mode,
        ),
        lcu=lcu,
        game=game,
        match_mode=match_mode,
        is_exact_match=is_exact_match,
        match_reason=match_reason,
        candidate_count=1,
    )


def _download_manifest_payload(
    *,
    manifest: PatcherManifest,
    file_path: str,
    temp_dir: Path,
) -> bytes:
    """下载 manifest 内指定文件并返回字节内容."""
    target_file = manifest.files.get(file_path)
    if target_file is None:
        raise ManifestInspectionError(f"manifest {manifest.file} 中不存在目标文件: {file_path}")
    return _LcuVersionResolver._download_manifest_file(
        manifest=manifest,
        target_file=target_file,
        temp_dir=temp_dir,
    )


def _extract_game_version_from_metadata(payload: bytes) -> str:
    """从 `content-metadata.json` 中提取 GAME 版本."""
    try:
        content = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestInspectionError("无法解析 content-metadata.json") from exc

    version = content.get("version")
    if not isinstance(version, str) or not version:
        raise ManifestInspectionError("content-metadata.json 缺少有效的 version 字段")
    return version


def _extract_manifest_id_from_source(source: str) -> str:
    """从本地路径或 URL 中提取 manifest_id."""
    parsed = urlparse(source)
    if parsed.scheme and parsed.netloc:
        return parsed.path.rsplit("/", maxsplit=1)[-1].removesuffix(".manifest")
    return Path(source).name.removesuffix(".manifest")


def _require_manifest_version(manifest: ManifestRef) -> VersionInfo:
    """确保 manifest 已携带版本信息."""
    if manifest.version is None:
        raise ManifestInspectionError(f"manifest {manifest.url} 未携带可用版本信息")
    return manifest.version
