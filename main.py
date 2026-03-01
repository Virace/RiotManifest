# -*- coding: utf-8 -*-
# @Author  : Virace
# @Email   : Virace@aliyun.com
# @Site    : x-item.com
# @Software: Pycharm
# @Create  : 2024/3/12 22:19
# @Update  : 2024/9/9 16:02
# @Detail  : 

import sys
from typing import Union
from pathlib import Path

from riotmanifest import RiotGameData, WADExtractor
from loguru import logger

logger.configure(handlers=[dict(sink=sys.stdout, level="DEBUG")])
logger.enable("riotmanifest")
StrPath = Union[str, 'os.PathLike[str]']


def extra_test():
    we = WADExtractor(r"C:\Users\Virace\Downloads\DE515F568F4D9C73.manifest")
    data = we.extract_files(
        {
            "DATA/FINAL/Champions/Annie.wad.client": ['data/characters/Annie/skins/skin0.bin', 'data/characters/Annie/skins/skin1.bin', 'data/characters/Annie/skins/skin2.bin', 'data/characters/Annie/skins/skin3.bin', 'data/characters/Annie/skins/skin4.bin', 'data/characters/Annie/skins/skin5.bin', 'data/characters/Annie/skins/skin6.bin', 'data/characters/Annie/skins/skin7.bin', 'data/characters/Annie/skins/skin8.bin', 'data/characters/Annie/skins/skin9.bin', 'data/characters/Annie/skins/skin10.bin', 'data/characters/Annie/skins/skin11.bin', 'data/characters/Annie/skins/skin12.bin', 'data/characters/Annie/skins/skin13.bin', 'data/characters/Annie/skins/skin14.bin', 'data/characters/Annie/skins/skin15.bin', 'data/characters/Annie/skins/skin16.bin', 'data/characters/Annie/skins/skin17.bin', 'data/characters/Annie/skins/skin18.bin', 'data/characters/Annie/skins/skin19.bin', 'data/characters/Annie/skins/skin20.bin', 'data/characters/Annie/skins/skin21.bin', 'data/characters/Annie/skins/skin22.bin', 'data/characters/Annie/skins/skin23.bin', 'data/characters/Annie/skins/skin24.bin', 'data/characters/Annie/skins/skin25.bin', 'data/characters/Annie/skins/skin26.bin', 'data/characters/Annie/skins/skin27.bin', 'data/characters/Annie/skins/skin28.bin', 'data/characters/Annie/skins/skin29.bin', 'data/characters/Annie/skins/skin30.bin', 'data/characters/Annie/skins/skin31.bin', 'data/characters/Annie/skins/skin32.bin', 'data/characters/Annie/skins/skin33.bin', 'data/characters/Annie/skins/skin34.bin', 'data/characters/Annie/skins/skin35.bin', 'data/characters/Annie/skins/skin36.bin', 'data/characters/Annie/skins/skin37.bin', 'data/characters/Annie/skins/skin38.bin', 'data/characters/Annie/skins/skin39.bin', 'data/characters/Annie/skins/skin40.bin', 'data/characters/Annie/skins/skin41.bin', 'data/characters/Annie/skins/skin42.bin', 'data/characters/Annie/skins/skin43.bin', 'data/characters/Annie/skins/skin44.bin', 'data/characters/Annie/skins/skin45.bin', 'data/characters/Annie/skins/skin46.bin', 'data/characters/Annie/skins/skin47.bin', 'data/characters/Annie/skins/skin48.bin', 'data/characters/Annie/skins/skin49.bin', 'data/characters/Annie/skins/skin50.bin', 'data/characters/Annie/skins/skin51.bin', 'data/characters/Annie/skins/skin52.bin', 'data/characters/Annie/skins/skin53.bin', 'data/characters/Annie/skins/skin54.bin', 'data/characters/Annie/skins/skin55.bin', 'data/characters/Annie/skins/skin56.bin', 'data/characters/Annie/skins/skin57.bin', 'data/characters/Annie/skins/skin58.bin', 'data/characters/Annie/skins/skin59.bin', 'data/characters/Annie/skins/skin60.bin', 'data/characters/Annie/skins/skin61.bin', 'data/characters/Annie/skins/skin62.bin', 'data/characters/Annie/skins/skin63.bin', 'data/characters/Annie/skins/skin64.bin', 'data/characters/Annie/skins/skin65.bin', 'data/characters/Annie/skins/skin66.bin', 'data/characters/Annie/skins/skin67.bin', 'data/characters/Annie/skins/skin68.bin', 'data/characters/Annie/skins/skin69.bin', 'data/characters/Annie/skins/skin70.bin', 'data/characters/Annie/skins/skin71.bin', 'data/characters/Annie/skins/skin72.bin', 'data/characters/Annie/skins/skin73.bin', 'data/characters/Annie/skins/skin74.bin', 'data/characters/Annie/skins/skin75.bin', 'data/characters/Annie/skins/skin76.bin', 'data/characters/Annie/skins/skin77.bin', 'data/characters/Annie/skins/skin78.bin', 'data/characters/Annie/skins/skin79.bin', 'data/characters/Annie/skins/skin80.bin', 'data/characters/Annie/skins/skin81.bin', 'data/characters/Annie/skins/skin82.bin', 'data/characters/Annie/skins/skin83.bin', 'data/characters/Annie/skins/skin84.bin', 'data/characters/Annie/skins/skin85.bin', 'data/characters/Annie/skins/skin86.bin', 'data/characters/Annie/skins/skin87.bin', 'data/characters/Annie/skins/skin88.bin', 'data/characters/Annie/skins/skin89.bin', 'data/characters/Annie/skins/skin90.bin', 'data/characters/Annie/skins/skin91.bin', 'data/characters/Annie/skins/skin92.bin', 'data/characters/Annie/skins/skin93.bin', 'data/characters/Annie/skins/skin94.bin', 'data/characters/Annie/skins/skin95.bin', 'data/characters/Annie/skins/skin96.bin', 'data/characters/Annie/skins/skin97.bin', 'data/characters/Annie/skins/skin98.bin', 'data/characters/Annie/skins/skin99.bin', 'data/characters/Annie/skins/skin100.bin'],
            # "DATA/FINAL/Champions/Ahri.wad.client": [
            #     "data/characters/Ahri/skins/skin0.bin",
            #     "data/characters/Ahri/skins/skin1.bin",
            #     "data/characters/Ahri/skins/skin2.bin",
            #     "data/characters/Ahri/skins/skin3.bin",
            # ]
        }
    )
    print(len(data))

def test_game_data():
    rgd = RiotGameData()
    rgd.load_lcu_data()
    rgd.load_game_data()
    print(rgd.latest_lcu())
    print(rgd.latest_game())
    print(rgd.available_lcu_regions())
    print(rgd.available_game_regions())


def main():
    test_game_data()

if __name__ == "__main__":
    # asyncio.run(main())
    main()
    # manifest = PatcherManifest(r"https://lol.secure.dyn.riotcdn.net/channels/public/releases/AB8447A8C9D41A42.manifest")
    # manifest = PatcherManifest(r"C:\Users\Virace\Downloads\AB8447A8C9D41A42.manifest",
    #                            save_path=r"H:\Programming\Python\PyManifest\temp")
    # for file in manifest.files.values():

    # chunks = file.chunks
    # for chunk in chunks:
    #     print(chunk.size, chunk.target_size)
    # print(len(chunks))
    #
    # # 将chunks按bundle_id分组， 去重
    # chunk_group = {}
    # for chunk in chunks:
    #     if chunk.bundle.bundle_id not in chunk_group:
    #         chunk_group[chunk.bundle.bundle_id] = set()
    #     chunk_group[chunk.bundle.bundle_id].add(chunk)
    # print(len(chunk_group))
    # if file.name == 'Plugins/rcp-be-lol-game-data/default-assets.wad':
    #     logger.info(f"开始下载...{file.name}")
    #     manifest.download_file(file)
    #     logger.info(f"下载完毕...{file.name}")

