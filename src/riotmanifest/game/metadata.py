"""LCU/GAME 元数据拉取与解析函数."""

from __future__ import annotations

import re
from typing import Any

from loguru import logger

from riotmanifest.utils.http_client import http_get_json

LCU_URL = "https://clientconfig.rpg.riotgames.com/api/v1/config/public?namespace=keystone.products.league_of_legends.patchlines"
GAME_URL_TEMPLATE = "https://sieve.services.riotcdn.net/api/v1/products/lol/version-sets/{region}?q[platform]=windows"


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


def parse_game_release(release: dict[str, Any]) -> dict[str, str] | None:
    """解析 GAME 单条 release，返回版本与下载地址."""
    release_meta = release.get("release") or {}
    labels = release_meta.get("labels") or {}

    artifact_type = first_value((labels.get("riot:artifact_type_id") or {}).get("values"))
    if artifact_type != "lol-game-client":
        return None

    platforms = (labels.get("platform") or {}).get("values") or []
    if "windows" not in platforms:
        return None

    version_raw = first_value((labels.get("riot:artifact_version_id") or {}).get("values"))
    download_url = (release.get("download") or {}).get("url")
    if not version_raw or not download_url:
        return None

    return {"version": version_raw.split("+", 1)[0], "url": download_url}


def fetch_lcu_data(*, url: str = LCU_URL) -> dict[str, dict[str, str]]:
    """从 LCU 接口拉取并返回区域到版本信息映射."""
    logger.debug("正在加载 LCU 数据")
    data = http_get_json(url)
    if not isinstance(data, dict):
        logger.warning("LCU 接口返回异常数据类型: {}", type(data))
        return {}

    lcu_data: dict[str, dict[str, str]] = {}
    for patchline in data.values():
        platforms = (patchline or {}).get("platforms") or {}
        win_data = platforms.get("win") or {}
        for config in win_data.get("configurations") or []:
            config_id = config.get("id")
            patch_url = config.get("patch_url")
            theme_manifest = ((config.get("metadata") or {}).get("theme_manifest")) or ""
            if not config_id or not patch_url or not isinstance(theme_manifest, str):
                continue

            parts = theme_manifest.split("/")
            if len(parts) < 3:
                continue
            lcu_data[str(config_id)] = {"version": parts[-3], "url": str(patch_url)}

    logger.debug("LCU 数据加载完成，可用区域数量={}", len(lcu_data))
    return lcu_data


def fetch_game_data(
    region: str,
    *,
    url_template: str = GAME_URL_TEMPLATE,
) -> list[dict[str, str]]:
    """从 GAME 接口拉取并返回给定区域的候选版本列表."""
    url = url_template.format(region=region)
    data = http_get_json(url)
    releases = data.get("releases", []) if isinstance(data, dict) else []

    parsed: list[dict[str, str]] = []
    for release in releases:
        if not isinstance(release, dict):
            continue
        item = parse_game_release(release)
        if item is not None:
            parsed.append(item)

    logger.debug("GAME 区域 {} 加载完成，候选版本数={}", region, len(parsed))
    return parsed
