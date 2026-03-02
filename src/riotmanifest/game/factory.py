"""LCU/GAME 元数据加载与提取器构造."""

from __future__ import annotations

from os import PathLike
from typing import Any

from loguru import logger

from riotmanifest.extractor import WADExtractor
from riotmanifest.game.metadata import (
    GAME_URL_TEMPLATE as _GAME_URL_TEMPLATE,
)
from riotmanifest.game.metadata import (
    LCU_URL as _LCU_URL,
)
from riotmanifest.game.metadata import (
    fetch_game_data,
    fetch_lcu_data,
    version_key,
)
from riotmanifest.manifest import PatcherManifest

StrPath = str | PathLike[str]


class RiotGameData:
    """整合 LCU/GAME 版本信息并按需构造提取器。."""

    LCU_URL = _LCU_URL
    GAME_URL_TEMPLATE = _GAME_URL_TEMPLATE

    def __init__(self):
        """初始化区域数据缓存."""
        self._lcu_data: dict[str, dict[str, str]] = {}
        self._game_data: dict[str, list[dict[str, str]]] = {}

    def load_lcu_data(self) -> None:
        """加载并解析 LCU 数据。."""
        self._lcu_data = fetch_lcu_data(url=self.LCU_URL)

    def load_game_data(self, regions: list[str] | None = None) -> None:
        """加载并解析 GAME 数据。."""
        regions = regions or ["EUW1", "PBE1"]
        logger.debug("正在加载 GAME 数据，区域={}", regions)

        for region in regions:
            self._game_data[region] = fetch_game_data(
                region=region,
                url_template=self.GAME_URL_TEMPLATE,
            )

    def latest_lcu(self, region: str = "EUW") -> dict[str, str] | None:
        """获取指定区域最新 LCU 配置信息。."""
        return self._lcu_data.get(region)

    def latest_game(self, region: str = "EUW1") -> dict[str, str] | None:
        """获取指定区域最新 GAME 发布信息。."""
        releases = self._game_data.get(region)
        if not releases:
            return None
        return max(releases, key=lambda item: version_key(item["version"]))

    def build_lcu_extractor(
        self,
        region: str = "EUW",
        *,
        manifest_path: StrPath = "",
        **extractor_kwargs: Any,
    ) -> WADExtractor:
        """为指定 LCU 区域构造 WADExtractor。."""
        latest = self.latest_lcu(region)
        if latest is None:
            raise ValueError(f"区域 {region} 没有可用的 LCU 数据，请先 load_lcu_data()")
        manifest = PatcherManifest(file=latest["url"], path=manifest_path)
        return WADExtractor(manifest, **extractor_kwargs)

    def build_game_extractor(
        self,
        region: str = "EUW1",
        *,
        manifest_path: StrPath = "",
        **extractor_kwargs: Any,
    ) -> WADExtractor:
        """为指定 GAME 区域构造 WADExtractor。."""
        latest = self.latest_game(region)
        if latest is None:
            raise ValueError(f"区域 {region} 没有可用的 GAME 数据，请先 load_game_data(regions=[...])")
        manifest = PatcherManifest(file=latest["url"], path=manifest_path)
        return WADExtractor(manifest, **extractor_kwargs)

    def available_lcu_regions(self) -> list[str]:
        """返回当前可用 LCU 区域列表。."""
        return sorted(self._lcu_data.keys())

    def available_game_regions(self) -> list[str]:
        """返回当前可用 GAME 区域列表。."""
        return sorted(self._game_data.keys())
