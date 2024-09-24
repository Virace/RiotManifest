# -*- coding: utf-8 -*-
# @Author  : Virace
# @Email   : Virace@aliyun.com
# @Site    : x-item.com
# @Software: Pycharm
# @Create  : 2024/9/5 12:02
# @Update  : 2024/9/9 21:46
# @Detail  : RiotGameData

from typing import Dict, List, Optional, Union

import requests
from loguru import logger

from riotmanifest.extractor import WADExtractor


class RiotGameData:
    """
    整合 LCU 和 GAME 数据的管理类。
    """

    LCU_URL = "https://clientconfig.rpg.riotgames.com/api/v1/config/public?namespace=keystone.products.league_of_legends.patchlines"
    GAME_URL_TEMPLATE = (
        "https://sieve.services.riotcdn.net/api/v1/products/lol/version-sets/{region}?q[platform]=windows"
    )

    def __init__(self):
        self._lcu_data: Dict[str, Dict[str, Union[str, Dict]]] = {}
        self._game_data: Dict[str, List[Dict[str, str]]] = {}
        self.lcu_wad = None
        self.game_wad = None

    def load_lcu_data(self) -> None:
        """加载并解析 LCU 数据。"""
        logger.debug("正在加载 LCU 数据...")
        response = requests.get(self.LCU_URL)
        response.raise_for_status()  # 确保请求成功
        lcu_data = response.json()

        for name, patchline in lcu_data.items():
            for config_json in patchline["platforms"]["win"]["configurations"]:
                version = config_json["metadata"]["theme_manifest"].split("/")[-3]
                self._lcu_data[config_json["id"]] = {"version": version, "url": config_json["patch_url"]}

        self.lcu_wad = WADExtractor(self.latest_lcu()['url'])
        logger.debug("LCU 数据加载完成")

    def load_game_data(self, regions: Optional[List[str]] = None) -> None:
        """加载并解析 GAME 数据。"""
        if regions is None:
            regions = ["EUW1", "PBE1"]

        logger.debug(f"正在加载 GAME 数据，区域: {regions}...")
        for region in regions:
            url = self.GAME_URL_TEMPLATE.format(region=region)
            response = requests.get(url)
            response.raise_for_status()  # 确保请求成功
            game_data = response.json()

            self._game_data[region] = [
                {
                    "version": release["release"]["labels"]["riot:artifact_version_id"]["values"][0].split("+")[0],
                    "url": release["download"]["url"],
                }
                for release in game_data.get("releases", [])
                if release["release"]["labels"]["riot:artifact_type_id"]["values"][0] == "lol-game-client"
                and "windows" in release["release"]["labels"]["platform"]["values"]
            ]
        
        self.game_wad = WADExtractor(self.latest_game()['url'])
        logger.debug("GAME 数据加载完成")

    def latest_lcu(self, region: str = "EUW") -> Optional[Dict[str, str]]:
        """获取指定区域的最新版本 LCU 配置信息。"""
        return self._lcu_data.get(region)

    def latest_game(self, region: str = "EUW1") -> Optional[Dict[str, str]]:
        """获取指定区域的最新版本 GAME 发布信息。"""
        if region in self._game_data:
            sorted_releases = sorted(self._game_data[region], key=lambda r: [int(v) for v in r["version"].split(".")])
            return sorted_releases[-1] if sorted_releases else None
        logger.error(f"未找到区域 {region} 的数据")
        return None

    def available_lcu_regions(self) -> List[str]:
        """返回当前可用的 LCU 区域列表。"""
        return list(self._lcu_data.keys())

    def available_game_regions(self) -> List[str]:
        """返回当前可用的 GAME 区域列表。"""
        return list(self._game_data.keys())

