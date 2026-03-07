# RiotManifest

[![PyPI](https://img.shields.io/pypi/v/riotmanifest?logo=pypi&logoColor=white)](https://pypi.org/project/riotmanifest/)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://pypi.org/project/riotmanifest/)
[![PyPI - Downloads](https://img.shields.io/pypi/dm/riotmanifest?logo=pypi&logoColor=white)](https://pypi.org/project/riotmanifest/)
[![Release Workflow](https://github.com/Virace/RiotManifest/actions/workflows/python-publish.yml/badge.svg)](https://github.com/Virace/RiotManifest/actions/workflows/python-publish.yml)
[![GitHub Release](https://img.shields.io/github/v/release/Virace/RiotManifest?logo=github)](https://github.com/Virace/RiotManifest/releases)

Riot 提供的 manifest 解析、并发下载、WAD 按需提取与差异分析工具。

- [安装](#安装)
- [30 秒上手](#30-秒上手)
- [常见任务](#常见任务)
- [实践建议](#实践建议)
- [文档导航](#文档导航)
- [维护者](#维护者)
- [感谢](#感谢)
- [许可证](#许可证)

## 安装

```bash
pip3 install riotmanifest
```

## 30 秒上手

最常见的下载入口是 `PatcherManifest`：

```python
import asyncio
from riotmanifest import PatcherManifest


async def main() -> None:
    manifest = PatcherManifest(
        "https://lol.secure.dyn.riotcdn.net/channels/public/releases/CB3A1B2A17ED9AAB.manifest",
        path="./out",
        bundle_url="https://lol.dyn.riotcdn.net/channels/public/bundles/",
    )

    files = list(manifest.filter_files(flag="zh_CN", pattern="wad.client"))
    await manifest.download_files_concurrently(files)


if __name__ == "__main__":
    asyncio.run(main())
```

默认并发为 `16`。

## 常见任务

### 1. 下载 manifest 中的一批文件

- 入口：`PatcherManifest`
- 适合：批量下载 `wad.client`、语言资源、配置文件

### 2. 从 WAD 中按需提取少量文件

```python
from riotmanifest import PatcherManifest, WADExtractor

manifest = PatcherManifest(manifest_url, path="")
extractor = WADExtractor(manifest)

data = extractor.extract_files(
    {
        "DATA/FINAL/Champions/Ahri.wad.client": [
            "data/characters/ahri/skins/skin0.bin",
        ]
    }
)
```

### 3. 比较两个版本的 manifest / WAD 差异

```python
from riotmanifest import diff_manifests, diff_wad_headers

manifest_report = diff_manifests(old_manifest, new_manifest, flags="zh_CN", pattern="wad.client")
wad_report = diff_wad_headers(manifest_report=manifest_report)
```

### 4. 获取当前 live 且版本规则明确的一对 LCU / GAME manifest

```python
from riotmanifest import RiotGameData

rgd = RiotGameData()
pair = rgd.resolve_live_manifest_pair("EUW")

print(str(pair.version))  # 例如 16.5
print(pair.lcu.url)
print(pair.game.url)
```

> 重要：
> `RiotGameData` 的默认 `match_mode` 现在就是
> `VersionMatchMode.IGNORE_REVISION`。
> Riot 的 live 发布经常出现“GAME 先更新、LCU 稍后更新”的窗口期；
> 同时 `patchsieve` 只暴露当前滚动窗口中的少量 GAME 候选，不是完整历史库。
> 因此 `STRICT` 要求 `normalized_build` 完全一致，在 live 场景里大概率直接失败。
> 如果你只处理 `wad.client`、语言包、贴图、音频等资源文件，通常可以忽略修订号，
> 只按补丁版本匹配。只有在你明确要求 EXE / DLL / build 级完全一致时，
> 才建议使用 `STRICT` 并自行处理失败。

## 实践建议

### 下载

- 默认并发 `16` 是当前推荐值。
- 网络或磁盘较弱时可降到 `8~12`。
- 机器配置较好且网络稳定时，可尝试 `16~24`。

### WAD 提取

- `WADExtractor` 适合“少量小文件按需提取”。
- 若单个 WAD 目标文件很多，通常更建议先下载完整 WAD 再本地处理。

### 差异分析

- 大多数情况下，先 `diff_manifests`，再按需进入 `diff_wad_headers`。
- `resolve_wad_diff_paths()` 默认推荐 `extractor` 模式。
- 仅在“需要完整落盘、后续离线复用、磁盘空间充足”时考虑 `download_root_wad`。

### RiotGameData

- 默认 `match_mode` 现在就是 `IGNORE_REVISION`。
- Riot live 常见顺序是 GAME 先更新、LCU 稍后更新，因此 `STRICT` 在 live 场景里大概率失败。
- 如果你只处理资源文件（如 `wad.client`、语言包、贴图、音频），通常可以忽略修订号。
- `IGNORE_REVISION` 当前会优先选择“同补丁内不高于 LCU build 的最大 GAME 候选”，避免误选比 LCU 更新的 GAME build。
- 如果你明确要“同补丁内无条件取最新 GAME 修订”，可改用 `VersionMatchMode.PATCH_LATEST`。
- 只有在你要求 EXE / DLL / build 级完全一致时，才建议使用 `STRICT`。
- `str(pair.version)` 默认输出补丁号，例如 `16.5`；如需精确显示，可读取 `pair.version.lcu.display_version` 或 `pair.version.game.display_version`。

## 文档导航

详细文档已按功能拆分：

- [docs/API.md](docs/API.md)：文档导航页
- [docs/manifest.md](docs/manifest.md)：Manifest 下载参考
- [docs/extractor.md](docs/extractor.md)：WADExtractor 参考
- [docs/diff.md](docs/diff.md)：差异分析参考
- [docs/game.md](docs/game.md)：RiotGameData / 版本对象参考
- [docs/TESTING.md](docs/TESTING.md)：测试与基准说明

## 维护者

**Virace**

- github: [Virace](https://github.com/Virace)
- blog: [孤独的未知数](https://x-item.com)

## 感谢

- [@CommunityDragon](https://github.com/CommunityDragon/CDTB), **CDTB**

## 许可证

[GPLv3](LICENSE)
