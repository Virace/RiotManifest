# RiotManifest

[![PyPI](https://img.shields.io/pypi/v/riotmanifest?logo=pypi&logoColor=white)](https://pypi.org/project/riotmanifest/)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://pypi.org/project/riotmanifest/)
[![PyPI - Downloads](https://img.shields.io/pypi/dm/riotmanifest?logo=pypi&logoColor=white)](https://pypi.org/project/riotmanifest/)
[![Release Workflow](https://github.com/Virace/RiotManifest/actions/workflows/python-publish.yml/badge.svg)](https://github.com/Virace/RiotManifest/actions/workflows/python-publish.yml)
[![GitHub Release](https://img.shields.io/github/v/release/Virace/RiotManifest?logo=github)](https://github.com/Virace/RiotManifest/releases)

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

- [docs/API.md](https://github.com/Virace/RiotManifest/blob/main/docs/API.md)

## 维护者

**Virace**

- github: [Virace](https://github.com/Virace)
- blog: [孤独的未知数](https://x-item.com)

## 感谢

- [@CommunityDragon](https://github.com/CommunityDragon/CDTB), **CDTB**

## 许可证

[GPLv3](LICENSE)
