# Manifest 下载参考

## 作用范围

本文件说明 Manifest 下载主线，也就是：

- `PatcherManifest`
- `PatcherFile`
- `PatcherChunk`
- `PatcherBundle`
- `DownloadProgress`
- 下载相关错误语义

如果你只想快速运行一段示例，先看 [README.md](../README.md)。

## 核心对象关系

### `PatcherManifest`

入口对象，负责两件事：

1. 解析本地或远程 manifest
2. 组织全局并发下载调度

定义位置：
- [src/riotmanifest/manifest.py](../src/riotmanifest/manifest.py)

### `PatcherFile`

表示 manifest 中的一个文件条目。  
它持有：

- 文件名
- 文件大小
- 文件 flags
- 该文件由哪些 `PatcherChunk` 组成

### `PatcherChunk`

表示 bundle 内的一个 chunk。  
它记录：

- `chunk_id`
- 所属 `bundle`
- 在 bundle 内的偏移
- 压缩后大小
- 解压后大小

### `PatcherBundle`

表示一个 bundle 以及它的 chunk 列表。

### `DownloadProgress`

下载进度快照对象，由调度器在运行中回调给用户。  
适合日志、进度条和吞吐统计。

## `PatcherManifest`

### 构造参数

```python
PatcherManifest(
    file,
    path,
    bundle_url="https://lol.dyn.riotcdn.net/channels/public/bundles/",
    concurrency_limit=16,
    max_retries=5,
)
```

参数说明：

- `file`
  - 本地 manifest 路径，或远程 manifest URL
  - 不能为空
- `path`
  - 下载输出目录
- `bundle_url`
  - bundle 基础地址
  - 一般保持默认即可
- `concurrency_limit`
  - 默认 bundle 级并发数
  - 当前默认值是 `16`
- `max_retries`
  - 单个 bundle 作业的最大重试次数

### 重要默认值

`PatcherManifest` 内部还定义了几组下载策略常量：

- `DEFAULT_GAP_TOLERANCE = 32 * 1024`
- `DEFAULT_MAX_RANGES_PER_REQUEST = 30`
- `DEFAULT_MIN_TRANSFER_SPEED_BYTES = 50 * 1024`
- `DEFAULT_BASE_TIMEOUT_SECONDS = 30`
- `DEFAULT_MAX_TIMEOUT_SECONDS = 180`
- `DEFAULT_SOCK_READ_TIMEOUT_SECONDS = 45`

这些常量不建议在外部直接依赖其具体值，但理解它们有助于理解下载器行为：

- 小间隔 chunk 会合并为同一次 range 请求
- 单个请求最多合并一定数量的 ranges
- 超时会根据作业大小与速度动态调整

## 下载行为

当前下载链路采用“全局任务调度”：

1. 先把目标文件展开为 `chunk`
2. 按 `chunk_id` 全局去重
3. 再按 `bundle_id` 聚合
4. 对相邻 chunk 合并 range
5. 并发请求 bundle
6. 解压后按偏移写入目标文件

这样做的目标是：

- 避免同一 chunk 被重复下载
- 避免同一 chunk 被重复解压
- 减少 HTTP 请求数量
- 降低文件句柄反复开关的开销

## 公开方法

### `filter_files(pattern=None, flag=None)`

用途：

- 从 manifest 中筛选目标文件

常见用法：

```python
files = list(manifest.filter_files(flag="zh_CN", pattern="wad.client"))
```

规则：

- `pattern`
  - 正则匹配文件名
- `flag`
  - 可传单个字符串或字符串列表
  - 用于匹配 manifest 的语言/标签字段

### `download_files_concurrently(...)`

签名重点：

```python
await manifest.download_files_concurrently(
    files,
    concurrency_limit=None,
    raise_on_error=True,
    progress_callback=None,
    progress_interval_seconds=1.0,
)
```

参数说明：

- `files`
  - 要下载的 `PatcherFile` 列表
- `concurrency_limit`
  - 本次调用临时覆盖默认并发
- `raise_on_error`
  - 任一 bundle 作业失败时是否抛出批量异常
- `progress_callback`
  - 进度回调
- `progress_interval_seconds`
  - 周期进度上报间隔
  - `None` 或 `<=0` 表示禁用周期上报

返回值：

- 与输入文件顺序一致的 `tuple[bool, ...]`

### `file_output(file)`

返回目标文件在输出目录中的绝对路径。  
这是辅助方法，通常在需要自定义日志或调试时才用。

### `preallocate_file(file)`

提前按最终大小占位目标文件。  
通常由下载主线内部使用。

## `PatcherFile`

### 主要字段

- `name`
- `size`
- `link`
- `flags`
- `chunks`
- `chunk_hash_types`

### 常用方法

#### `hexdigest()`

返回一个基于 chunk 组成的摘要。  
注意：

- 这不是文件字节内容的传统哈希
- 它更适合判断“manifest 视角下是否同一文件内容”

#### `download_file(...)`

单文件下载入口。  
内部仍然走 manifest 的全局调度器，不是孤立的旧式下载逻辑。

#### `download_chunk(chunk)`

同步下载并解压一个 chunk。  
更多是底层能力，不建议优先用它拼业务流程。

## `DownloadProgress`

字段说明：

- `phase`
  - 当前阶段，例如周期上报、bundle 完成等
- `total_jobs`
- `finished_jobs`
- `succeeded_jobs`
- `failed_jobs`
- `total_bytes`
- `finished_bytes`
- `progress`
- `elapsed_seconds`
- `average_speed_bytes_per_sec`
- `bundle_id`
  - 若当前事件与某个 bundle 绑定，则会带上 bundle id

典型回调用法：

```python
def on_progress(progress: DownloadProgress) -> None:
    print(progress.phase, progress.progress, progress.average_speed_bytes_per_sec)
```

## 错误语义

### `DownloadError`

适用于：

- HTTP 拉取失败
- chunk 大小不匹配

### `DecompressError`

适用于：

- zstd 解压失败
- chunk 解压后大小与目标大小不匹配

### `DownloadBatchError`

适用于：

- 并发批量下载时，一个或多个 bundle job 失败

## 推荐调用路径

最常用的下载路径通常是：

```python
import asyncio
from riotmanifest import PatcherManifest


async def main() -> None:
    manifest = PatcherManifest(manifest_url, path="./out")
    files = list(manifest.filter_files(flag="zh_CN", pattern="wad.client"))
    await manifest.download_files_concurrently(files)


asyncio.run(main())
```

若你还需要：

- 少量资源按需提取：转到 [extractor.md](./extractor.md)
- 做版本比较：转到 [diff.md](./diff.md)
- 获取当前 live 版本一致的一对清单：转到 [game.md](./game.md)
