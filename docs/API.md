# RiotManifest 文档导航

![](https://img.shields.io/badge/python-%3E%3D3.10-blue)

本文档不再承担“所有内容堆在一个文件里”的角色，而是作为文档导航页使用。

如果你是第一次接触项目，建议按这个顺序阅读：

1. [README.md](../README.md)：最快理解项目能做什么、怎么开始用、有哪些实践建议
2. [manifest.md](./manifest.md)：Manifest 下载主线
3. [extractor.md](./extractor.md)：WAD 按需提取
4. [diff.md](./diff.md)：Manifest / WAD 差异分析与 BIN 路径回填
5. [game.md](./game.md)：`LeagueManifestResolver`、live 一致对、版本对象与错误语义
6. [TESTING.md](./TESTING.md)：测试脚本、验证方式与基准说明

## 按任务选文档

### 我只想尽快开始用

- 先看 [README.md](../README.md)

### 我只想下载 manifest 里的文件

- 看 [manifest.md](./manifest.md)

### 我只想从 WAD 中提取少量资源

- 看 [extractor.md](./extractor.md)

### 我想比较两个版本的差异

- 看 [diff.md](./diff.md)

### 我想拿到当前 live 且版本规则明确的一对 LCU/GAME manifest

- 看 [game.md](./game.md)
- 其中 live 场景示例默认显式使用 `VersionMatchMode.IGNORE_REVISION`

## 包根导出一览

当前 `riotmanifest` 根包主要按以下功能导出对象：

### Manifest 下载主线

- `PatcherManifest`
- `PatcherFile`
- `PatcherChunk`
- `PatcherBundle`
- `DownloadProgress`
- `DownloadError`
- `DownloadBatchError`
- `DecompressError`

### WAD 提取

- `WADExtractor`

### 差异分析

- `diff_manifests`
- `diff_wad_headers`
- `resolve_wad_diff_paths`
- `ManifestDiffSummary`
- `ManifestDiffEntry`
- `ManifestMovedEntry`
- `ManifestDiffReport`
- `WADSectionSignature`
- `WADSectionDiffEntry`
- `WADFileDiffEntry`
- `WADHeaderDiffSummary`
- `WADHeaderDiffReport`
- `WADPathProvider`
- `ManifestBinPathProvider`

### LeagueManifestResolver 与版本对象

- `LeagueManifestResolver`
- `RiotGameData`（兼容旧名，实例化时会发出 `FutureWarning`，计划在 `v3.0.0` 删除）
- `VersionMatchMode`
- `VersionDisplayMode`
- `VersionInfo`
- `ManifestRef`
- `ResolvedVersion`
- `LiveManifestPair`
- `RiotGameDataError`
- `LiveConfigNotFoundError`
- `LcuVersionUnavailableError`
- `ConsistentGameManifestNotFoundError`

### 其他基础对象

- `BinaryParser`
- `HttpClientError`

## 文档划分原则

本轮文档重构采用两层结构：

- `README.md`
  - 只放最快理解项目与最快上手所需内容
  - 同时保留高频实践建议
- `docs/*.md`
  - 按功能拆分
  - 对常量、类、函数、返回对象、错误语义和调用策略做详细说明

如果后续继续扩展功能，应优先把内容落到对应功能文档，而不是重新堆回导航页。
