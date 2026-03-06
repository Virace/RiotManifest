# Manifest / WAD 差异分析参考

## 作用范围

本文件说明差异分析主线：

- `diff_manifests`
- `diff_wad_headers`
- `resolve_wad_diff_paths`
- `ManifestBinPathProvider`
- `WADPathProvider`
- 各类 diff 数据模型

## 总体流程

推荐顺序通常是：

1. `diff_manifests`
2. `diff_wad_headers`
3. `resolve_wad_diff_paths`

也就是：

- 先定位哪些文件变了
- 再定位某个 WAD 里哪些 section 变了
- 最后把内部 hash / BIN 路径尽量回填成人可读路径

## `diff_manifests`

定义位置：
- [src/riotmanifest/diff/manifest_diff.py](../src/riotmanifest/diff/manifest_diff.py)

### 作用

比较两个 manifest 的文件级差异。

### 常用参数

- `old_manifest`
- `new_manifest`
- `flags`
- `pattern`
- `target_files`
- `include_unchanged`
- `detect_moves`
- `overlap_warning_threshold`

### 典型输出

返回 `ManifestDiffReport`，其中包含：

- `summary`
- `added`
- `removed`
- `changed`
- `unchanged`
- `moved`
- `warnings`

## `ManifestDiffSummary`

概要统计对象，关注：

- 新增文件数
- 删除文件数
- 修改文件数
- 未变化文件数
- 移动文件数

适合直接用于：

- CLI 摘要输出
- CI 报告
- JSON 汇总

## `ManifestDiffEntry`

表示单个 manifest 文件差异项。  
常见字段语义包括：

- `path`
- `status`
- `old_*`
- `new_*`
- `section_diffs`

其中 `section_diffs` 很重要：  
它让 manifest 级条目和后续 WAD 级条目可以挂在同一主线上，而不是割裂成两套结构。

## `diff_wad_headers`

定义位置：
- [src/riotmanifest/diff/wad_header_diff.py](../src/riotmanifest/diff/wad_header_diff.py)

### 作用

比较两个版本中同一路径 WAD 的内部 section 差异。

### 建议输入

最推荐直接传 `manifest_report`，因为这样可以复用前一步的运行时上下文，避免重复初始化 manifest。

### 常见场景

- 某个英雄资源包内部到底改了哪些节
- 某个地图 WAD 里具体变动发生在哪些 section

## `WADHeaderDiffSummary`

WAD 级别差异概要统计，适合做：

- WAD 变动数摘要
- 内部 section 状态统计

## `WADFileDiffEntry`

表示单个 WAD 文件在头部层面的差异。  
通常会包含：

- `wad_path`
- `status`
- `section_diffs`

## `WADSectionDiffEntry`

表示 WAD 内部单个 section 的差异项。  
这是后续做 BIN 路径回填的关键落点。

## `resolve_wad_diff_paths`

定义位置：
- [src/riotmanifest/diff/wad_path_resolution.py](../src/riotmanifest/diff/wad_path_resolution.py)

### 作用

尽可能把 WAD section 对应的 hash / BIN 路径回填成人类可读路径。

### 两种 BIN 数据来源

- `extractor`
  - 默认模式
  - 使用 `WADExtractor` 按需提取目标 BIN
- `download_root_wad`
  - 先下载 root WAD
  - 再从本地提取 BIN

### 选择建议

- 默认选 `extractor`
  - 适合目标分散、数量不大、以回填为主的常规场景
- 仅在以下情况考虑 `download_root_wad`
  - 明确需要完整落盘
  - 需要后续离线复用
  - 磁盘空间充足

### 默认缓存目录

`download_root_wad` 模式默认会使用：

- `.cache/wad_root_wad_bin_resolution`

这是实现细节，不建议外部强依赖，但知道它有助于排查磁盘占用与清理策略。

## 路径提供器

### `WADPathProvider`

协议接口。  
只要求实现：

- `collect_paths(wad_path: str) -> tuple[str, ...]`

### `ManifestBinPathProvider`

默认实现，按命名规则为英雄 WAD / 地图 WAD 提供候选 BIN 路径。

构造参数重点：

- `max_skin_id`
- `include_champion_root_bins`
- `include_map_bins`
- `global_paths`
- `wad_bin_paths`

适合：

- 大多数按命名规则可推断的 WAD
- 想在默认规则上附加少量额外路径

## 常见调用模板

### 文件级差异

```python
from riotmanifest import diff_manifests

report = diff_manifests(old_manifest, new_manifest, flags="zh_CN", pattern="wad.client")
print(report.summary)
```

### WAD 内部差异

```python
from riotmanifest import diff_manifests, diff_wad_headers

manifest_report = diff_manifests(old_manifest, new_manifest, flags="zh_CN")
wad_report = diff_wad_headers(manifest_report=manifest_report)
```

### 路径回填

```python
from riotmanifest import ManifestBinPathProvider, resolve_wad_diff_paths

with ManifestBinPathProvider(max_skin_id=100) as provider:
    resolved_report = resolve_wad_diff_paths(
        wad_report,
        path_provider=provider,
        bin_data_source_mode="extractor",
    )
```

## 输出与导出

`ManifestDiffReport` 和 `WADHeaderDiffReport` 都支持 JSON 导出。  
最常见的方式是：

```python
report.dump_pretty_json("out/report.json", collapse_equal_pairs=True)
```

## 何时继续深入

如果你只是想“知道有哪些文件变了”，通常停在 `diff_manifests` 就够了。  
只有在这些场景里，才需要继续做 WAD / BIN 层分析：

- 英雄技能、数值、配置路径回填
- 地图资源内部结构定位
- 资源差异审计
