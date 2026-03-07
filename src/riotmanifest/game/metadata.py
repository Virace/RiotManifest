"""LCU/GAME 元数据拉取与解析函数."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from loguru import logger

from riotmanifest.utils.http_client import http_get_json

PATCHLINES_URL = (
    "https://clientconfig.rpg.riotgames.com/api/v1/config/public?namespace=keystone.products.league_of_legends.patchlines"
)
VERSION_SET_URL_TEMPLATE = "https://sieve.services.riotcdn.net/api/v1/products/lol/version-sets/{region}"

# 兼容旧命名，后续内部实现统一使用更贴近语义的常量名。
LCU_URL = PATCHLINES_URL
GAME_URL_TEMPLATE = VERSION_SET_URL_TEMPLATE


def first_value(values: Any) -> str | None:
    """从列表值中提取第一个元素并转为字符串."""
    if isinstance(values, list) and values:
        return str(values[0])
    return None


def version_key(version: str) -> tuple[tuple[int, object], ...]:
    """把版本字符串标准化为可比较的排序键."""
    parts: list[tuple[int, object]] = []
    for token in re.split(r"[.\-+_]", version):
        if not token:
            continue
        if token.isdigit():
            parts.append((0, int(token)))
        else:
            parts.append((1, token.lower()))
    return tuple(parts)


def extract_manifest_id(url: str) -> str:
    """从 manifest URL 中提取 manifest_id."""
    manifest_name = urlparse(url).path.rsplit("/", maxsplit=1)[-1]
    return manifest_name.removesuffix(".manifest")


def extract_theme_patch_version(theme_manifest: str) -> str | None:
    """从 theme_manifest 中提取补丁版本提示."""
    if not isinstance(theme_manifest, str):
        return None

    match = re.search(r"/releases/(\d+\.\d+\.\d+)/theme/", theme_manifest)
    if match is None:
        match = re.search(r"/theme/(\d+\.\d+)/", theme_manifest)
    if match is None:
        return None
    return match.group(1)


def parse_game_release(
    release: dict[str, Any],
    *,
    artifact_type: str = "lol-game-client",
    platform: str = "windows",
) -> dict[str, str] | None:
    """解析 GAME 单条 release，返回版本与下载地址.

    Args:
        release: 单条 release 原始 JSON。
        artifact_type: 目标 artifact 类型。
        platform: 目标平台。

    Returns:
        仅保留版本与下载地址的标准化结果；若不匹配则返回 `None`。
    """
    release_meta = release.get("release") or {}
    labels = release_meta.get("labels") or {}

    current_artifact_type = first_value((labels.get("riot:artifact_type_id") or {}).get("values"))
    if current_artifact_type != artifact_type:
        return None

    platforms = (labels.get("platform") or {}).get("values") or []
    if platform not in platforms:
        return None

    version_raw = first_value((labels.get("riot:artifact_version_id") or {}).get("values"))
    download_url = (release.get("download") or {}).get("url")
    if not version_raw or not download_url:
        return None

    return {"version": version_raw.split("+", 1)[0], "url": download_url}


def _extract_patchline_id(namespace_key: str) -> str | None:
    """从 clientconfig namespace 键中提取 patchline 标识."""
    if not isinstance(namespace_key, str):
        return None
    patchline_id = namespace_key.rsplit(".", maxsplit=1)[-1].strip()
    return patchline_id or None


def _build_game_data_url(
    *,
    region: str,
    url_template: str,
    artifact_type: str,
    platform: str,
    published: bool | None,
) -> str:
    """构造带筛选参数的 GAME version-set 请求地址."""
    parsed_url = urlparse(url_template.format(region=region))
    query_params = dict(parse_qsl(parsed_url.query, keep_blank_values=True))
    query_params["q[artifact_type_id]"] = artifact_type
    query_params["q[platform]"] = platform
    if published is not None:
        query_params["q[published]"] = str(published).lower()
    return urlunparse(parsed_url._replace(query=urlencode(query_params)))


def _extract_launcher_region(arguments: Any) -> str | None:
    """从启动参数中提取 `--region=` 指定的大区标识."""
    if not isinstance(arguments, list):
        return None

    for argument in arguments:
        if not isinstance(argument, str):
            continue
        if argument.startswith("--region="):
            region = argument.split("=", maxsplit=1)[-1].strip()
            if region:
                return region
    return None


def _collect_region_aliases(*aliases: str | None, extra_aliases: list[str] | None = None) -> list[str]:
    """汇总并去重同一条配置可识别的大区别名."""
    ordered_aliases: list[str] = []
    seen: set[str] = set()
    for alias in [*aliases, *(extra_aliases or [])]:
        if not isinstance(alias, str):
            continue
        normalized_alias = alias.strip().upper()
        if not normalized_alias or normalized_alias in seen:
            continue
        ordered_aliases.append(normalized_alias)
        seen.add(normalized_alias)
    return ordered_aliases


def fetch_lcu_data(*, url: str = LCU_URL) -> dict[str, dict[str, dict[str, Any]]]:
    """从 LCU 接口拉取并返回 patchline 到配置映射."""
    logger.debug("正在加载 LCU 数据")
    data = http_get_json(url)
    if not isinstance(data, dict):
        logger.warning("LCU 接口返回异常数据类型: {}", type(data))
        return {}

    lcu_data: dict[str, dict[str, dict[str, Any]]] = {}
    for patchline_key, patchline in data.items():
        patchline_id = _extract_patchline_id(patchline_key)
        if patchline_id is None:
            continue

        patchline_configs: dict[str, dict[str, Any]] = {}
        platforms = (patchline or {}).get("platforms") or {}
        win_data = platforms.get("win") or {}
        for config in win_data.get("configurations") or []:
            config_id = config.get("id")
            patch_url = config.get("patch_url")
            theme_manifest = ((config.get("metadata") or {}).get("theme_manifest")) or ""
            if not config_id or not patch_url:
                continue

            region_data = config.get("region_data") or {}
            default_region = region_data.get("default_region")
            available_regions = [
                region
                for region in (region_data.get("available_regions") or [])
                if isinstance(region, str)
            ]
            launcher_region = _extract_launcher_region(
                ((config.get("launcher") or {}).get("arguments")) or []
            )

            game_version_set = ""
            game_artifact_type = ""
            game_platform = ""
            patch_artifacts = config.get("patch_artifacts") or []
            for artifact in patch_artifacts:
                if not isinstance(artifact, dict):
                    continue
                if artifact.get("id") != "game_client" or artifact.get("type") != "patchsieve":
                    continue

                patchsieve = artifact.get("patchsieve") or {}
                parameters = patchsieve.get("parameters") or {}
                version_set = patchsieve.get("version_set")
                artifact_type = parameters.get("artifact_type_id")
                platform = parameters.get("platform")
                if isinstance(version_set, str):
                    game_version_set = version_set
                if isinstance(artifact_type, str):
                    game_artifact_type = artifact_type
                if isinstance(platform, str):
                    game_platform = platform
                break

            canonical_region = (
                (default_region if isinstance(default_region, str) else "")
                or launcher_region
                or str(config_id)
                or game_version_set
            )
            region_aliases = _collect_region_aliases(
                str(config_id),
                default_region if isinstance(default_region, str) else None,
                launcher_region,
                game_version_set or None,
                extra_aliases=available_regions,
            )
            patchline_configs[str(config_id)] = {
                "patchline": patchline_id,
                "canonical_region": canonical_region.upper(),
                "lcu_config_id": str(config_id).upper(),
                "launcher_region": launcher_region.upper() if launcher_region else "",
                "url": str(patch_url),
                "version_hint": extract_theme_patch_version(theme_manifest) or "",
                "manifest_id": extract_manifest_id(str(patch_url)),
                "game_version_set": game_version_set.upper(),
                "game_artifact_type": game_artifact_type,
                "game_platform": game_platform,
                "region_aliases": region_aliases,
            }

        if patchline_configs:
            lcu_data[patchline_id] = patchline_configs

    logger.debug(
        "LCU 数据加载完成，patchline 数量={}，配置数量={}",
        len(lcu_data),
        sum(len(configs) for configs in lcu_data.values()),
    )
    return lcu_data


def fetch_game_data(
    region: str,
    *,
    url_template: str = GAME_URL_TEMPLATE,
    artifact_type: str = "lol-game-client",
    platform: str = "windows",
    published: bool | None = None,
) -> list[dict[str, str]]:
    """从 GAME 接口拉取并返回给定区域的候选版本列表.

    Args:
        region: version-set 区域标识。
        url_template: 请求地址模板。
        artifact_type: 目标 artifact 类型。
        platform: 目标平台。
        published: 是否只请求已发布条目；`None` 表示不附加该筛选。

    Returns:
        匹配目标条件的候选列表。
    """
    url = _build_game_data_url(
        region=region,
        url_template=url_template,
        artifact_type=artifact_type,
        platform=platform,
        published=published,
    )
    data = http_get_json(url)
    releases = data.get("releases", []) if isinstance(data, dict) else []

    parsed: list[dict[str, str]] = []
    for release in releases:
        if not isinstance(release, dict):
            continue
        item = parse_game_release(
            release,
            artifact_type=artifact_type,
            platform=platform,
        )
        if item is not None:
            parsed.append(item)

    logger.debug("GAME 区域 {} 加载完成，候选版本数={}", region, len(parsed))
    return parsed
