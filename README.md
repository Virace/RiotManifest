# RiotManifest

![](https://img.shields.io/badge/python-%3E%3D3.10-blue)

riot 提供的 manifest 文件解析与下载工具。

- [安装](#安装)
- [快速使用](#快速使用)
- [完整 API 文档](#完整-api-文档)
- [维护者](#维护者)
- [感谢](#感谢)
- [许可证](#许可证)

## 安装

```bash
pip3 install riotmanifest
```

## 快速使用

### 下载（异步并发，推荐）

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

默认并发为 `16`。可通过 `PatcherManifest(..., concurrency_limit=...)` 或 `download_files_concurrently(..., concurrency_limit=...)` 调整。

## 完整 API 文档

完整用法（进度回调、WAD 提取、Manifest/WAD diff、性能基线）请见：

- [docs/API.md](docs/API.md)

## 维护者

**Virace**

- github: [Virace](https://github.com/Virace)
- blog: [孤独的未知数](https://x-item.com)

## 感谢

- [@CommunityDragon](https://github.com/CommunityDragon/CDTB), **CDTB**
- JetBrains 提供开发环境支持

  <a href="https://www.jetbrains.com/?from=kratos-pe" target="_blank"><img src="https://cdn.jsdelivr.net/gh/virace/kratos-pe@main/jetbrains.svg"></a>

## 许可证

[GPLv3](LICENSE)
