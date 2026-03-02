"""LCU/GAME 元数据加载与提取器构造."""

from __future__ import annotations

import re
from typing import Any

from loguru import logger

from riotmanifest.extractor import WADExtractor
from riotmanifest.http_client import http_get_json


class RiotGameData:
    """整合 LCU/GAME 版本信息并按需构造提取器。."""

    LCU_URL = "https://clientconfig.rpg.riotgames.com/api/v1/config/public?namespace=keystone.products.league_of_legends.patchlines"
    GAME_URL_TEMPLATE = (
        "https://sieve.services.riotcdn.net/api/v1/products/lol/version-sets/{region}?q[platform]=windows"
    )

    def __init__(self):
        """初始化区域数据缓存."""
        self._lcu_data: dict[str, dict[str, str]] = {}
        self._game_data: dict[str, list[dict[str, str]]] = {}

    @staticmethod
    def _first_value(values: Any) -> str | None:
        if isinstance(values, list) and values:
            return str(values[0])
        return None

    @staticmethod
    def _version_key(version: str) -> tuple[tuple[int, object], ...]:
        parts: list[tuple[int, object]] = []
        for token in re.split(r"[.\-+_]", version):
            if not token:
                continue
            if token.isdigit():
                parts.append((0, int(token)))
            else:
                parts.append((1, token.lower()))
        return tuple(parts)

    @staticmethod
    def _parse_game_release(release: dict[str, Any]) -> dict[str, str] | None:
        release_meta = release.get("release") or {}
        labels = release_meta.get("labels") or {}

        artifact_type = RiotGameData._first_value((labels.get("riot:artifact_type_id") or {}).get("values"))
        if artifact_type != "lol-game-client":
            return None

        platforms = (labels.get("platform") or {}).get("values") or []
        if "windows" not in platforms:
            return None

        version_raw = RiotGameData._first_value((labels.get("riot:artifact_version_id") or {}).get("values"))
        download_url = (release.get("download") or {}).get("url")
        if not version_raw or not download_url:
            return None

        return {"version": version_raw.split("+", 1)[0], "url": download_url}

    def load_lcu_data(self) -> None:
        """加载并解析 LCU 数据。."""
        logger.debug("正在加载 LCU 数据")
        data = http_get_json(self.LCU_URL)
        if not isinstance(data, dict):
            logger.warning("LCU 接口返回异常数据类型: {}", type(data))
            return

        self._lcu_data.clear()
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
                version = parts[-3]
                self._lcu_data[str(config_id)] = {"version": version, "url": str(patch_url)}

        logger.debug("LCU 数据加载完成，可用区域数量={}", len(self._lcu_data))

    def load_game_data(self, regions: list[str] | None = None) -> None:
        """加载并解析 GAME 数据。."""
        regions = regions or ["EUW1", "PBE1"]
        logger.debug("正在加载 GAME 数据，区域={}", regions)

        for region in regions:
            url = self.GAME_URL_TEMPLATE.format(region=region)
            data = http_get_json(url)
            releases = data.get("releases", []) if isinstance(data, dict) else []

            parsed: list[dict[str, str]] = []
            for release in releases:
                if not isinstance(release, dict):
                    continue
                item = self._parse_game_release(release)
                if item is not None:
                    parsed.append(item)

            self._game_data[region] = parsed
            logger.debug("GAME 区域 {} 加载完成，候选版本数={}", region, len(parsed))

    def latest_lcu(self, region: str = "EUW") -> dict[str, str] | None:
        """获取指定区域最新 LCU 配置信息。."""
        return self._lcu_data.get(region)

    def latest_game(self, region: str = "EUW1") -> dict[str, str] | None:
        """获取指定区域最新 GAME 发布信息。."""
        releases = self._game_data.get(region)
        if not releases:
            return None
        return max(releases, key=lambda item: self._version_key(item["version"]))

    def build_lcu_extractor(self, region: str = "EUW", **extractor_kwargs: Any) -> WADExtractor:
        """为指定 LCU 区域构造 WADExtractor。."""
        latest = self.latest_lcu(region)
        if latest is None:
            raise ValueError(f"区域 {region} 没有可用的 LCU 数据，请先 load_lcu_data()")
        return WADExtractor(latest["url"], **extractor_kwargs)

    def build_game_extractor(self, region: str = "EUW1", **extractor_kwargs: Any) -> WADExtractor:
        """为指定 GAME 区域构造 WADExtractor。."""
        latest = self.latest_game(region)
        if latest is None:
            raise ValueError(f"区域 {region} 没有可用的 GAME 数据，请先 load_game_data(regions=[...])")
        return WADExtractor(latest["url"], **extractor_kwargs)

    def available_lcu_regions(self) -> list[str]:
        """返回当前可用 LCU 区域列表。."""
        return sorted(self._lcu_data.keys())

    def available_game_regions(self) -> list[str]:
        """返回当前可用 GAME 区域列表。."""
        return sorted(self._game_data.keys())
