# WADExtractor 参考

## 作用范围

本文件说明 `WADExtractor` 的职责、适用场景、缓存与预取行为。

定义位置：
- [src/riotmanifest/extractor/wad_extractor.py](../src/riotmanifest/extractor/wad_extractor.py)

## 适用场景

`WADExtractor` 适合：

- 从 WAD 中提取少量小文件
- 批量读取多个 BIN / JSON / 配置类小文件
- 避免为了几个小文件而下载完整 WAD

不适合：

- 单个 WAD 需要提取很多目标文件
- 你明确需要完整 WAD 落盘
- 你后续需要反复离线处理整包内容

这类场景下，通常更适合先下载完整 WAD 再本地解包。

## 核心思路

`WADExtractor` 不直接依赖本地 WAD 文件。  
它会：

1. 基于 manifest 找到目标 `wad.client`
2. 读取 WAD 头
3. 定位目标内部文件所在 section
4. 只下载必要的 chunk
5. 拼接、切片、返回目标数据

## 构造参数

```python
WADExtractor(
    manifest,
    bundle_url=None,
    cache_max_bytes=128 * 1024 * 1024,
    cache_max_entries=512,
    retry_limit=5,
    prefetch_chunk_concurrency=16,
    recommended_max_targets_per_wad=120,
)
```

参数说明：

- `manifest`
  - 必须是 `PatcherManifest`
- `bundle_url`
  - 若不传，则复用 manifest 的 bundle URL
- `cache_max_bytes`
  - chunk 解压缓存上限
- `cache_max_entries`
  - chunk 缓存最大条目数
- `retry_limit`
  - 单个 chunk 下载重试次数
- `prefetch_chunk_concurrency`
  - 预取时的 chunk 并发数
- `recommended_max_targets_per_wad`
  - 单个 WAD 建议提取目标数阈值
  - 超过后会跳过预取，但不阻止提取主流程

## 关键默认值

- `DEFAULT_PREFETCH_CHUNK_CONCURRENCY = 16`
- `DEFAULT_RECOMMENDED_MAX_TARGETS_PER_WAD = 120`

## 公开方法

### `extract_files(wad_file_paths)`

输入结构：

```python
{
    "DATA/FINAL/Champions/Aatrox.wad.client": [
        "data/characters/aatrox/skins/skin0.bin",
        "data/characters/aatrox/skins/skin1.bin",
    ]
}
```

返回结构：

```python
{
    "DATA/FINAL/Champions/Aatrox.wad.client": {
        "data/characters/aatrox/skins/skin0.bin": b"...",
        "data/characters/aatrox/skins/skin1.bin": b"...",
    }
}
```

如果某个目标未找到或提取失败，对应值会是 `None`。

### `extract_files_to_disk(wad_file_paths, output_dir)`

与 `extract_files()` 类似，但返回的是落盘路径而不是字节。

适合：

- 你需要后续交给外部工具处理
- 你想保留中间产物

### `get_wad_header(wad_file)`

返回解析后的 WAD 头对象。  
这通常用于更底层的调试和差异分析流程。

### `clear_cache()` / `cache_stats()`

- `clear_cache()`
  - 清空 chunk 解压缓存
- `cache_stats()`
  - 返回当前缓存统计

## 缓存行为

缓存键基于：

- `bundle_id`
- `chunk_id`

因此它天然支持跨文件、跨 WAD 的 chunk 复用。  
这也是它在“多个小文件、多个 WAD、共享 chunk”场景中仍然有效的原因之一。

## 预取行为

### 单 WAD 预取

当目标数不多时，提取器会预先分析目标 section 所需 chunk，并做并发预热。

### 跨 WAD 全局预取

当前实现还支持跨 WAD 的全局去重预取：

1. 先解析所有目标 WAD
2. 汇总目标 section
3. 按 `(bundle_id, chunk_id)` 去重
4. 并发预取

这样可避免多个 WAD / 多个目标共用 chunk 时重复拉取。

### 跳过预取的条件

若某个 WAD 的目标数超过 `recommended_max_targets_per_wad`，会跳过预取。  
原因不是功能不支持，而是：

- 预取收益开始下降
- 请求数量与调度成本上升
- 此时整包下载往往更划算

## 错误语义

常见错误来源：

- 目标 WAD 不存在
- WAD 头读取失败
- chunk 下载失败
- chunk 解压失败
- 目标内部路径不存在

很多情况下，`extract_files()` 不会直接把所有错误向外抛出，而是把失败项映射成 `None`，以便批量处理继续进行。

## 推荐调用路径

### 读取少量 BIN

```python
from riotmanifest import PatcherManifest, WADExtractor

manifest = PatcherManifest(manifest_url, path="")
extractor = WADExtractor(manifest)

payload = extractor.extract_files(
    {
        "DATA/FINAL/Champions/Ahri.wad.client": [
            "data/characters/ahri/skins/skin0.bin",
        ]
    }
)
```

### 直接写盘

```python
outputs = extractor.extract_files_to_disk(
    {
        "DATA/FINAL/Maps/Shipping/Map11/Map11.wad.client": [
            "data/maps/shipping/map11/map11.bin",
        ]
    },
    output_dir="./out_wad",
)
```

## 与差异分析的关系

`WADExtractor` 也是 `resolve_wad_diff_paths()` 默认的 BIN 数据来源。  
如果你在做：

- WAD 内部差异
- BIN 路径回填

请继续看 [diff.md](./diff.md)。
