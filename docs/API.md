# RiotManifest API 文档

![](https://img.shields.io/badge/python-%3E%3D3.10-blue)

本文档包含完整接口用法、进度回调、WAD 按需提取与 Manifest/WAD 差异分析。

## 目录

- [安装](#安装)
- [PatcherManifest 下载接口](#patchermanifest-下载接口)
- [DownloadProgress 进度与速度回调](#downloadprogress-进度与速度回调)
- [WADExtractor 按需提取](#wadextractor-按需提取)
- [Manifest / WAD 差异分析](#manifest--wad-差异分析)
- [RiotGameData](#riotgamedata)
- [测试与基准文档](#测试与基准文档)

## 安装

```bash
pip3 install riotmanifest
```

## PatcherManifest 下载接口

当前下载链路默认走全局并发下载（推荐），并支持：

- 按 `ChunkID` 全局去重，避免重复下载与重复解压
- 按 Bundle 聚合与 multi-range 请求，减少 HTTP 往返
- 文件句柄池按偏移写入，降低 open/close 开销
- chunk 解压后哈希校验（`param_index -> hash_type`）

默认并发数为 `16`，可通过 `PatcherManifest(..., concurrency_limit=...)` 调整，也可在调用 `download_files_concurrently` 时临时覆盖。

```python
import asyncio
from riotmanifest import PatcherManifest


async def main() -> None:
    bundle_url = "https://lol.dyn.riotcdn.net/channels/public/bundles/"
    manifest = PatcherManifest(
        "https://lol.secure.dyn.riotcdn.net/channels/public/releases/CB3A1B2A17ED9AAB.manifest",
        path="./out",
        bundle_url=bundle_url,
        concurrency_limit=16,
    )

    files = list(manifest.filter_files(flag="zh_CN", pattern="wad.client"))
    await manifest.download_files_concurrently(files)


if __name__ == "__main__":
    asyncio.run(main())
```

调参建议：

- 网络/磁盘较弱：`8~12`
- 机器配置较好且网络稳定：`16~24`

## DownloadProgress 进度与速度回调

支持按时间周期上报，同时保留每个 bundle job 事件。

```python
import asyncio
from riotmanifest import DownloadProgress, PatcherManifest


def on_progress(progress: DownloadProgress) -> None:
    speed_mb = progress.average_speed_bytes_per_sec / (1024 * 1024)
    print(
        f"[{progress.phase}] "
        f"jobs={progress.finished_jobs}/{progress.total_jobs} "
        f"bytes={progress.finished_bytes}/{progress.total_bytes} "
        f"speed={speed_mb:.2f}MB/s "
        f"bundle={progress.bundle_id}"
    )


async def main() -> None:
    manifest = PatcherManifest(
        "https://lol.secure.dyn.riotcdn.net/channels/public/releases/CB3A1B2A17ED9AAB.manifest",
        path="./out",
    )
    files = list(manifest.filter_files(flag="zh_CN", pattern="wad.client"))
    await manifest.download_files_concurrently(
        files,
        progress_callback=on_progress,
        progress_interval_seconds=1.0,
    )


if __name__ == "__main__":
    asyncio.run(main())
```

## WADExtractor 按需提取

该方式无需下载完整 WAD 文件，直接从 manifest 中计算目标文件位置并提取。

```python
from riotmanifest import PatcherManifest
from riotmanifest.extractor import WADExtractor

manifest = PatcherManifest("DE515F568F4D9C73.manifest", path="")
extractor = WADExtractor(manifest)

data = extractor.extract_files(
    {
        "DATA/FINAL/Champions/Aatrox.wad.client": [
            "data/characters/aatrox/skins/skin0.bin",
            "data/characters/aatrox/skins/skin1.bin",
        ],
        "DATA/FINAL/Champions/Ahri.wad.client": [
            "data/characters/Ahri/skins/skin0.bin",
            "data/characters/Ahri/skins/skin1.bin",
        ],
    }
)
print(len(data))
```

注意事项：

- 适合“少量小文件按需提取”
- 当单个 WAD 目标文件很多时，建议改为“先下载完整 WAD，再本地解包”
- 提取器内置小文件批量优化（chunk 受限并发预取），超过建议数量会跳过预取并告警

可调参数：

```python
we = WADExtractor(
    manifest,
    prefetch_chunk_concurrency=16,
    recommended_max_targets_per_wad=120,
)
```

直接写盘：

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

## Manifest / WAD 差异分析

### Manifest 文件级差异

```python
from riotmanifest import diff_manifests

report = diff_manifests(
    "https://lol.secure.dyn.riotcdn.net/channels/public/releases/9FE07DA11C89FD5E.manifest",
    "https://lol.secure.dyn.riotcdn.net/channels/public/releases/BA80B75282F55531.manifest",
    flags="zh_CN",
    pattern="wad.client",
)
print(report.summary)
print([item.path for item in report.changed])

# 导出 JSON
report.dump_pretty_json("out/manifest_diff.json", collapse_equal_pairs=True)
```

### WAD 头部差异

建议流程：先 `diff_manifests` 找到变化 WAD，再调用 `diff_wad_headers` 定位 WAD 内部差异。

```python
from riotmanifest import diff_manifests, diff_wad_headers

manifest_report = diff_manifests(
    old_manifest,
    new_manifest,
    flags="zh_CN",
    target_files=["DATA/FINAL/Champions/Akshan.zh_CN.wad.client"],
    include_unchanged=True,
    detect_moves=False,
)

wad_report = diff_wad_headers(
    manifest_report=manifest_report,
    target_wad_files=["DATA/FINAL/Champions/Akshan.zh_CN.wad.client"],
    include_unchanged=False,
)

# 导出 JSON
wad_report.dump_pretty_json(
    "out/wad_header_diff.json",
    collapse_manifest_equal_pairs=True,
)
```

说明：

- `include_unchanged=False` 时，会同时过滤
  - 未变化的 WAD 文件条目
  - `section_diffs` 中 `status='unchanged'` 的内部条目
- `diff_wad_headers` 支持复用 `manifest_report` 的运行时上下文，避免重复初始化两个清单

### BIN 路径回填模式选择（`resolve_wad_diff_paths`）

`resolve_wad_diff_paths` 支持两种 BIN 数据来源：

- `extractor`（默认）：按需提取目标 BIN 所需数据
- `download_root_wad`：先下载 root WAD 再本地提取 BIN

#### 简单调用（默认 `extractor`）

```python
from riotmanifest import (
    ManifestBinPathProvider,
    diff_manifests,
    diff_wad_headers,
    resolve_wad_diff_paths,
)

manifest_report = diff_manifests(
    old_manifest,
    new_manifest,
    flags="zh_CN",
    target_files=["DATA/FINAL/Champions/Akshan.zh_CN.wad.client"],
    include_unchanged=False,
    detect_moves=False,
)
wad_report = diff_wad_headers(
    manifest_report=manifest_report,
    target_wad_files=["DATA/FINAL/Champions/Akshan.zh_CN.wad.client"],
    include_unchanged=False,
)

with ManifestBinPathProvider(max_skin_id=100) as provider:
    resolved_report = resolve_wad_diff_paths(
        wad_report,
        path_provider=provider,
        bin_data_source_mode="extractor",
    )

resolved_report.manifest_report.dump_pretty_json(
    "out/manifest_diff_with_section_paths.json",
    collapse_equal_pairs=True,
)
```

实践建议：

- 默认使用 `extractor`：适合“目标 BIN 分散、目标 WAD 数量不大”的日常 diff/回填场景
- 仅在“需要完整落盘、后续离线复用、磁盘空间充足”时考虑 `download_root_wad`

原因说明：

- 稀疏 BIN 场景下，`download_root_wad` 通常会额外引入整包下载与本地 I/O 成本
- 大体量连续下载（例如全量 `ja_JP + wad.client`）是另一类问题，吞吐可接近满带宽，但不等价于稀疏 BIN 回填场景

## RiotGameData

```python
from riotmanifest import RiotGameData, VersionDisplayMode, VersionMatchMode

rgd = RiotGameData()

# 默认严格匹配：返回当前 live 且版本一致的一对 LCU/GAME manifest
pair = rgd.resolve_live_manifest_pair("EUW")
print(str(pair.version))  # 默认输出 16.5
print(pair.lcu.url)
print(pair.game.url)
print(pair.is_exact_match)
print(pair.version.lcu.display_version)   # 16.5.751.1533
print(pair.version.game.display_version)  # 16.5.7511533

# 也可以只拿统一版本号对象
version = rgd.resolve_live_version("EUW")
print(str(version))  # 16.5

# 显式构造当前 live 的 GAME Extractor
extractor = rgd.build_game_extractor("EUW", cache_max_entries=256, manifest_path="")

# 当同补丁内存在仅 exe/dll 修订、资源未变化的情况时，
# 可显式放宽到“忽略修订号”模式
relaxed_pair = rgd.resolve_live_manifest_pair(
    "EUW",
    match_mode=VersionMatchMode.IGNORE_REVISION,
)
print(relaxed_pair.match_reason)
print(str(relaxed_pair.version))  # 默认仍输出补丁号
print(relaxed_pair.version.with_display_mode(VersionDisplayMode.LCU))
```

说明：

- `resolve_live_manifest_pair()` 是 `RiotGameData` 的主入口。
- `pair.version` 是统一版本号对象，默认字符串输出为“忽略修订号”的补丁版本，例如 `16.5`。
- `pair.version.lcu` 与 `pair.version.game` 分别保留 LCU / GAME 的完整版本信息。
- 默认 `STRICT` 模式会先解析当前 live LCU manifest 的精确客户端版本，再在同一 live 配置对应的 GAME 候选中查找完全一致的 build。
- `IGNORE_REVISION` 模式仅要求 `major.minor` 一致，适合“同补丁内多次修订，但资源文件通常未变化”的场景。
- `build_game_extractor("EUW")` 现在以 LCU live 区域作为输入，而不是直接传 `EUW1` 这类 `version_set`。

旧接口迁移：

- 旧写法：
  - `load_lcu_data()` + `latest_lcu()`
  - `load_game_data()` + `latest_game()`
- 新写法：
  - `resolve_live_manifest_pair()`：直接获取当前 live 且版本规则明确的一对 manifest
  - `build_game_extractor()`：直接基于当前 live 一致对构造 GAME extractor
  - `get_live_lcu_manifest()` / `list_live_game_candidates()`：仅在你确实需要拆开处理底层数据时使用

## 测试与基准文档

测试脚本说明与基准命令已移至独立文档：`docs/TESTING.md`。
