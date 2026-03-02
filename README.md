# RiotManifest
![](https://img.shields.io/badge/python-%3E%3D3.10-blue)

riot提供的manifest文件进行解析下载

- [介绍](#介绍)
- [安装](#安装)
- [使用](#使用)
- [维护者](#维护者)
- [感谢](#感谢)
- [许可证](#许可证)


### 介绍
目前支持传入 URL 或本地文件路径，解析 manifest 并下载文件。

大部分代码都来自于[CommunityDragon/CDTB](https://github.com/CommunityDragon/CDTB)项目，感谢他们的工作。

当前下载链路默认走全局并发下载（推荐），并支持：
- 按 `ChunkID` 全局去重，避免重复下载与重复解压
- 按 Bundle 聚合与 multi-range 请求，减少 HTTP 往返
- 文件句柄池按偏移写入，降低 open/close 开销
- chunk 解压后哈希校验（`param_index -> hash_type`）

默认并发数为 `16`，可通过 `PatcherManifest(..., concurrency_limit=...)` 调整，也可在调用 `download_files_concurrently` 时临时覆盖。

### 安装
```shell
pip3 install riotmanifest
```

### 使用
- **异步并发下载（推荐，默认并发 16）**
```python
import asyncio
from riotmanifest import PatcherManifest


async def main():
    bundle_url = 'https://lol.dyn.riotcdn.net/channels/public/bundles/'
    manifest = PatcherManifest(
      r"https://lol.secure.dyn.riotcdn.net/channels/public/releases/CB3A1B2A17ED9AAB.manifest",
      path=r'E:\out',
      bundle_url=bundle_url)

    # 推荐：先按语言与文件名过滤后再下载
    files = list(manifest.filter_files(flag='zh_CN', pattern='wad.client'))

    # 不传 concurrency_limit 时，使用 manifest 默认并发（16）
    await manifest.download_files_concurrently(files)



if __name__ == '__main__':
    asyncio.run(main())
```

- 如果你的网络/磁盘较弱，可把并发改到 `8~12`；
- 如果机器配置较好且网络稳定，可尝试 `16~24` 并发。

### 性能基线（2026-03-02）
以下结果来自仓库内测试 `tests/test_manifest_download_speed.py`（真实网络集成测试）：

```bash
RIOT_PERF_RUN=1 ./scripts/_uv.sh run pytest -q -s tests/test_manifest_download_speed.py
```

本次结果（EUW1，默认并发 16，优先筛选 `filter_files(flag='zh_CN', pattern='wad.client')`）：
- manifest：`https://lol.secure.dyn.riotcdn.net/channels/public/releases/BA80B75282F55531.manifest`
- 样本：`files=92`，`planned=515.14MB`
- 吞吐：`63.61MB/s`（`elapsed=8.098s`）
- 调度：`jobs=126`，`ranges=142`，`unique_chunks=1410`

与 README 早期历史信息对比：
- 历史（2024）：文档标注“多并发下载不推荐，建议不超过 10”。
- 当前（2026）：默认并发已调整为 `16`，并发下载作为推荐路径，实测吞吐可稳定在几十 MB/s 量级（受网络波动影响）。


- WADExtractor

```python
from riotmanifest.extractor import WADExtractor

we = WADExtractor("DE515F568F4D9C73.manifest")
data = we.extract_files(
    {
        "DATA/FINAL/Champions/Aatrox.wad.client": [
            "data/characters/aatrox/skins/skin0.bin",
            "data/characters/aatrox/skins/skin1.bin",
            "data/characters/aatrox/skins/skin2.bin",
            "data/characters/aatrox/skins/skin3.bin",
        ],
        "DATA/FINAL/Champions/Ahri.wad.client": [
            "data/characters/Ahri/skins/skin0.bin",
            "data/characters/Ahri/skins/skin1.bin",
            "data/characters/Ahri/skins/skin2.bin",
            "data/characters/Ahri/skins/skin3.bin",
        ]
    }
)
print(len(data))
```
该方法无需下载完整WAD文件，直接从manifest中计算需要解包的文件位置，直接获取，减少网络请求。

可选：直接写入磁盘，避免在调用方持有大量 `bytes`：

```python
outputs = we.extract_files_to_disk(
    {
        "DATA/FINAL/Maps/Shipping/Map11/Map11.wad.client": [
            "data/maps/shipping/map11/map11.bin",
        ]
    },
    output_dir="./out_wad",
)
print(outputs)
```

- RiotGameData（显式构造 Extractor）

```python
from riotmanifest.game import RiotGameData

rgd = RiotGameData()
rgd.load_game_data(regions=["EUW1"])

# 不再在 load_game_data 中隐式创建 WADExtractor，改为按需显式构造
game_extractor = rgd.build_game_extractor("EUW1", cache_max_entries=256)
```


### 维护者
**Virace**
- github: [Virace](https://github.com/Virace)
- blog: [孤独的未知数](https://x-item.com)

### 感谢
- [@CommunityDragon](https://github.com/CommunityDragon/CDTB), **CDTB**

- 以及**JetBrains**提供开发环境支持
  
  <a href="https://www.jetbrains.com/?from=kratos-pe" target="_blank"><img src="https://cdn.jsdelivr.net/gh/virace/kratos-pe@main/jetbrains.svg"></a>

### 许可证

[GPLv3](LICENSE)
