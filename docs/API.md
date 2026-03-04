# RiotManifest API 文档

![](https://img.shields.io/badge/python-%3E%3D3.10-blue)

本文档包含完整接口用法、进度回调、WAD 按需提取、Manifest/WAD 差异分析与性能基线。

## 目录

- [安装](#安装)
- [PatcherManifest 下载接口](#patchermanifest-下载接口)
- [DownloadProgress 进度与速度回调](#downloadprogress-进度与速度回调)
- [WADExtractor 按需提取](#wadextractor-按需提取)
- [Manifest / WAD 差异分析](#manifest--wad-差异分析)
- [RiotGameData](#riotgamedata)
- [性能基线与基准测试](#性能基线与基准测试)

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
    prefetch_chunk_concurrency=6,
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

实践建议：

- 默认使用 `extractor`：适合“目标 BIN 分散、目标 WAD 数量不大”的日常 diff/回填场景
- 仅在“需要完整落盘、后续离线复用、磁盘空间充足”时考虑 `download_root_wad`

原因说明：

- 稀疏 BIN 场景下，`download_root_wad` 通常会额外引入整包下载与本地 I/O 成本
- 大体量连续下载（例如全量 `ja_JP + wad.client`）是另一类问题，吞吐可接近满带宽，但不等价于稀疏 BIN 回填场景

## RiotGameData

```python
from riotmanifest.game import RiotGameData

rgd = RiotGameData()
rgd.load_game_data(regions=["EUW1"])

# 显式构造 Extractor
extractor = rgd.build_game_extractor("EUW1", cache_max_entries=256, manifest_path="")
```

## 性能基线与基准测试

结果来自仓库内真实网络集成测试（同一日期，不同样本规模）。

常规压力样本：

```bash
RIOT_PERF_RUN=1 ./scripts/_uv.sh run pytest -q -s tests/test_manifest_download_speed.py
```

- manifest：`https://lol.secure.dyn.riotcdn.net/channels/public/releases/BA80B75282F55531.manifest`
- 样本：`files=92`，`planned=515.14MB`
- 吞吐：`63.61MB/s`（`elapsed=8.098s`）
- 调度：`jobs=126`，`ranges=142`，`unique_chunks=1410`

全量中文 `wad.client` 传输层对比：

```bash
RIOT_TRANSPORT_BENCH_RUN=1 RIOT_TRANSPORT_MODE=both ./scripts/_uv.sh run pytest -q -s tests/test_downloader_transport_compare.py
```

- manifest：`https://lol.secure.dyn.riotcdn.net/channels/public/releases/BA80B75282F55531.manifest`
- 样本：`files=212`，`planned=3605.73MB`
- `aiohttp`：`117.51MB/s`（`elapsed=30.685s`）
- `urllib3`：`113.00MB/s`（`elapsed=31.910s`）
- 速度比：`urllib3 / aiohttp = 0.962`

结论：

- 吞吐受网络波动影响明显，单次结果会波动
- 当前下载策略下已可基本跑满带宽
- `aiohttp` 与 `urllib3` 差距不大，当前样本下 `aiohttp` 略优

### downloader 多轮基准脚本（推荐）

为避免单次测试被网络抖动放大，建议用 `scripts/bench_downloader.py` 跑多轮并取中位数：

```bash
./scripts/_uv.sh run python scripts/bench_downloader.py \
  /mnt/c/Users/Virace/Downloads/BA80B75282F55531.manifest \
  --flag ja_JP \
  --pattern 'wad.client' \
  --concurrency 16 \
  --rounds 3 \
  --output-json out/downloader_bench_summary.json
```

可先 dry-run 只看计划：

```bash
./scripts/_uv.sh run python scripts/bench_downloader.py \
  /mnt/c/Users/Virace/Downloads/BA80B75282F55531.manifest \
  --flag ja_JP \
  --pattern 'wad.client' \
  --concurrency 16 \
  --rounds 3 \
  --dry-run
```

### 最新实测（2026-03-05）

测试条件：

- 样本：`ja_JP + wad.client`，`files=212`，`planned=3.555GiB`
- 并发：`16`
- 轮数：`3`
- 网络：`1000M` 宽带

结果：

- round1：`120.52MB/s`（`30.205s`）
- round2：`117.60MB/s`（`30.957s`）
- round3：`101.36MB/s`（`35.916s`）
- 中位数：`117.60MB/s`

下载中常见现象（非故障）：

- 单次结果会波动，后半段可能出现阶段性降速
- 并发开得更高不一定更快；在该样本下，`16` 已经是较稳妥配置
- 建议优先看多轮中位数，不要只看单次峰值
