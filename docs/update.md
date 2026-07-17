# 增量更新参考

## 作用范围

本文件说明增量更新主线：

- `ManifestUpdater`
- `SyncMode`
- `UpdateResult`
- `FileAction`
- `ManifestArchive` / `InstalledState`

## 核心语义

下载、修复、更新是同一条管线，不需要单独的"更新开关"：

| 本地状态 | 行为 |
|---|---|
| 目标文件不存在 | 全量下载 |
| 目标文件存在 | 按新清单 chunk 布局逐块验证，命中保留、miss 补洞下载 |
| 有旧清单（显式传入或本地存档） | 额外做文件级跳过：路径与 chunk 序列均未变的文件整个跳过 |

验证是"固定位置"的：按新清单里每个 chunk 的偏移在本地文件对应位置读取并哈希比对，
不做滑动窗口搜索。chunk 位置未变即命中，通常能覆盖版本间的绝大多数数据。

## 快速上手

```python
import asyncio
from riotmanifest import ManifestUpdater, PatcherManifest


async def main() -> None:
    manifest = PatcherManifest(
        "https://lol.secure.dyn.riotcdn.net/channels/public/releases/CB3A1B2A17ED9AAB.manifest",
        path="./out",
    )
    updater = ManifestUpdater(manifest)

    files = list(manifest.filter_files(flag="zh_CN", pattern="wad.client"))
    result = await updater.sync(files)

    print(f"下载 {result.downloaded_bytes} 字节，复用 {result.reused_bytes} 字节")


if __name__ == "__main__":
    asyncio.run(main())
```

首次运行没有旧清单时自动退化为"全量下载 + 逐文件验证"；
成功后清单会自动存档，下一次运行即自动进入增量模式。

## `ManifestUpdater`

构造参数：

- `manifest`：目标（新）清单，其 `path` 即输出目录。
- `old_manifest`：旧清单，支持路径 / URL / `PatcherManifest` 实例。
  不传时自动从输出目录的 `installed.json` 存档解析。

### `sync()`

- `files`：目标文件列表（如 `filter_files` 的结果）；`None` 表示全部文件。
- `mode`：见下方 `SyncMode`。
- `remove_deleted`：是否删除旧清单声明、新清单不含的文件（含移动后的旧路径），默认 `True`。
  只会删除旧清单声明过的路径，不碰目录里的陌生文件。
- `concurrency_limit` / `progress_callback` / `progress_interval_seconds`：透传给下载调度器。

返回 `UpdateResult`。部分文件失败不抛异常：失败文件记入 `failed`，
其旧文件保持原样，且本次不更新 `installed.json`（版本指针只在整批成功后推进）。

## `SyncMode`

- `AUTO`（默认）：上表语义。注意：被文件级跳过的文件不做本地验证
  （rman `--update` 同款权衡），怀疑本地损坏时用 `REPAIR`。
- `REPAIR`：不做文件级跳过，对全部目标文件逐 chunk 验证并补洞（修复）。
- `VERIFY_ONLY`：不做文件级跳过，只验证并报告缺失量，不写盘、不下载、不存档（dry-run）。
- `FORCE_FULL`：跳过一切验证，强制全量重下。

与 rman 的对应关系：`AUTO`≈`rman-dl --update`、`REPAIR`≈`rman-dl` 默认、
`VERIFY_ONLY`≈`--no-write`、`FORCE_FULL`≈`--no-verify`。

## 哈希类型的两个细节

- 清单中部分文件没有 params 条目（hash_type=0），但 chunk_id 仍是内容哈希。
  本地验证会对这类 chunk 穷举猜测算法（对齐 rman 的 `RChunk::hash_type`），
  命中即复用，不会盲目重下。
- Riot 会跨版本迁移哈希算法（实测 16.3 HKDF → 16.4 BLAKE3）。迁移边界上
  chunk_id 全部变化，文件级判同必然失效，但只要内容没变，
  猜测验证仍能 100% 复用本地数据（`scripts/e2e_update_real.py` 实测下载 0 字节）。
  内部 diff 使用 `strict` 模式，避免这类文件被误判为未变化。

## `UpdateResult`

- `actions`：path → 计划动作（`FileAction`：SKIP / PATCH / NEW / MOVE / REMOVE）。
- `downloaded_bytes`：网络补洞写入的解压域字节数。
- `reused_bytes`：本地验证命中/复制的解压域字节数。
- `missing_bytes`：未满足的缺口（`VERIFY_ONLY` 的缺失量，或失败文件的缺口）。
- `removed`：实际删除的路径列表。
- `failed`：下载失败的文件列表。

## 本地状态与磁盘布局

更新依赖两类本地信息，均不使用数据库：

```
<output>/.rman/installed.json              当前安装状态指针
<output>/.rman/manifests/<ID>.manifest     清单原文存档（保留当前 + 上一份）
<output>/<file>.rman-tmp                   下载过程中的临时文件（staging）
```

- 所有写入先落 staging，文件全部 chunk 就绪后原子替换到目标路径；
  中断或失败时旧文件保持完整。峰值磁盘占用约多出"单个在写文件"的大小。
- `installed.json` 的 schema 与 Go 项目 RiotManifestGo 共享，
  两个工具指向同一个游戏目录时状态可互认。

## 与 diff 模块的关系

`ManifestUpdater` 内部直接复用 `diff_manifests` 的文件级报告
（unchanged/changed/added/moved/removed → 跳过/补丁/新下/改名/删除）。
如果你同时需要"资源侧更新日志"，可以自己再调一次 `diff_manifests`
并使用其 JSON 导出，两者结论一致。

## 行为变更说明（相对 2.x 下载语义）

- `download_files_concurrently` 不再按"文件大小相同"跳过已存在文件，
  语义变为"给什么下什么"；跳过决策统一由 `ManifestUpdater` 承担。
  需要增量语义时请改用 `updater.sync()`。
- 下载写盘改为 staging + 原子替换：中断不再损坏已有文件。
