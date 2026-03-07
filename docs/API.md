# RiotManifest 文档导航

![](https://img.shields.io/badge/python-%3E%3D3.10-blue)

本文档是导航页，帮助你按任务找到对应说明。

## 推荐阅读顺序

1. `README.md`
   - 先看项目能做什么、怎么开始
2. `docs/manifest.md`
   - Manifest 下载主线
3. `docs/extractor.md`
   - WAD 按需提取
4. `docs/diff.md`
   - Manifest / WAD 差异分析与 BIN 路径回填
5. `docs/game.md`
   - `LeagueManifestResolver`、`LeagueManifestInspector`、区域语义、版本对象与兼容层
6. `docs/TESTING.md`
   - 测试、验证方式与基准说明

## 按任务选文档

### 我只想尽快开始用

- 看 `README.md`

### 我只想下载 manifest 里的文件

- 看 `docs/manifest.md`

### 我只想从 WAD 中提取少量资源

- 看 `docs/extractor.md`

### 我想比较两个版本的差异

- 看 `docs/diff.md`

### 我想拿到某个 LoL 区域当前可用的一对 LCU/GAME manifest

- 看 `docs/game.md`
- 公开入口统一只传一个 `region`
- 默认推荐 `VersionMatchMode.IGNORE_REVISION`

### 我想判断一个或两个 manifest 的类型与版本

- 看 `docs/game.md`
- `LeagueManifestInspector` 支持单清单识别和双清单配对

## 包根导出一览

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

### LoL manifest 解析与检查

主接口：

- `LeagueManifestResolver`
- `LeagueManifestInspector`
- `RegionConfigNotFoundError`
- `LcuVersionUnavailableError`
- `ConsistentGameManifestNotFoundError`
- `ManifestInspectionError`
- `VersionMatchMode`
- `VersionDisplayMode`
- `VersionInfo`
- `ManifestRef`
- `ResolvedVersion`
- `ResolvedManifestPair`

兼容层：

- `RiotGameData`
- `RiotGameDataError`
- `PatchlineConfigNotFoundError`
- `LiveConfigNotFoundError`
- `LiveManifestPair`

以上兼容层当前仍导出，但都计划在 `v3.0.0` 删除。

## 文档划分原则

- `README.md`
  - 放最快理解项目与最快上手所需内容
- `docs/*.md`
  - 按功能拆分，说明公开对象、返回类型、错误语义与调用建议

后续如继续扩展，请优先把说明补到对应功能文档，而不是重新堆回导航页。
