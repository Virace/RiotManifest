# 增量更新参考

## 作用范围

本文件说明增量更新主线：

- `ManifestUpdater`
- `SyncTarget` / `sync_many`
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
- `archive`（keyword-only，默认 `True`）：是否在输出目录维护 `.rman/` 存档
  与 installed.json。`False` 时不创建存档、不推进版本指针，旧清单只认显式
  传入——适合输出目录不归本库管辖的场景（如玩家的游戏目录，重装游戏时
  会被清空，使用侧自有增量基底时存档只是陌生残留）。

### `sync()`

- `files`：目标文件列表（如 `filter_files` 的结果）；`None` 表示全部文件。
- `mode`：见下方 `SyncMode`。
- `remove_deleted`：是否删除旧清单声明、新清单不含的文件（含移动后的旧路径），默认 `True`。
  只会删除旧清单声明过的路径，不碰目录里的陌生文件。
- `concurrency_limit` / `progress_callback` / `progress_interval_seconds`：透传给下载调度器。

返回 `UpdateResult`。部分文件失败不抛异常：失败文件记入 `failed`，
失败原因（bundle_id + 原始异常）记入 `failures`，
其旧文件保持原样，且本次不更新 `installed.json`（版本指针只在整批成功后推进）。

## 多清单联合同步 `sync_many`

同时同步多个清单（如 LCU + GAME）时，用 `sync_many` 合并下载调度：
单 worker 池、单一进度事件流，`DownloadProgress` 的 total/finished 为
跨清单合计——按字节加权的全局进度天然准确，无需使用侧自行聚合。

```python
from riotmanifest import SyncTarget, sync_many

results = await sync_many(
    [
        SyncTarget(manifest=manifest_lcu, files=files_lcu),
        SyncTarget(manifest=manifest_game, files=files_game, archive=False),
    ],
    concurrency_limit=16,           # 全局并发，缺省取各清单上限的最大值
    progress_callback=on_progress,  # 单一进度流，total 跨清单合计
)
```

返回与入参顺序一致的 `UpdateResult` 列表；部分失败不抛异常，
失败只影响所属清单的结果与存档推进。各清单保留自己的输出根与
`.rman` 存档语义（`archive` / `old_manifest` 可按清单指定）。
`mode` / `remove_deleted` 等其余参数语义与 `sync()` 相同，作用于全部目标。

## 进度事件生命周期

`progress_callback` 收到的 `DownloadProgress` 覆盖完整 sync 生命周期，
按 `phase` 区分阶段。各阶段 total/finished 口径不同（阶段内部自洽）：

| 层 | phase | 含义 | total/finished 口径 |
|---|---|---|---|
| 编排层 | `verify` | 本地逐 chunk 校验进行中（周期）与结束快照 | jobs=待验证文件数；bytes=解压域 |
| 编排层 | `plan_ready` | 计划确定，下载总量已知（哪怕为 0） | jobs=bundle 作业数；bytes=压缩域 |
| 调度层 | `start` / `tick` / `bundle_completed` / `bundle_failed` / `completed` / `failed` | 下载批次 | jobs=bundle 作业数；bytes=压缩域 |
| 编排层 | `finalize` | staging 提交 / 清理开始 | 同 plan_ready，finished=total |
| 编排层 | `sync_completed` / `sync_failed` | 全程终态（事件流最后一条） | 同 plan_ready，finished=total |

要点：

- 事件序列：`verify` → `plan_ready` → 下载阶段事件 → `finalize` →
  `sync_completed` / `sync_failed`。
- `plan_ready` 与下载阶段完全同口径（压缩域字节 / bundle 作业数），
  可直接用它渲染进度条分母；无任何需要下载的内容时也会发出（总量为 0），
  使用侧可区分"还没开始"与"没有要下的"。
- `verify` 阶段按已校验字节数报进度（REPAIR 全量校验不再静默）；
  周期间隔沿用 `progress_interval_seconds`，结束时必发一次
  `finished == total` 的快照。FORCE_FULL 跳过验证，无 `verify` 事件。
- VERIFY_ONLY 序列为 `verify` → `plan_ready`（报"需要下载多少"）→
  `sync_completed`，无下载与 `finalize`。
- `finalize` / 终态是里程碑事件：失败细节以下载阶段事件与
  `UpdateResult`（`failed` / `failures`）为准。

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
- `failures`：bundle 维度的失败详情（`BundleJobFailure` 列表）。
  `error` 为重试耗尽后的包装异常，底层原因（超时 / 连接重置 / HTTP 状态码等）
  沿 `error.__cause__` 链保留：

  ```python
  for failure in result.failures:
      print(f"bundle {failure.bundle_id:016X}: {failure.error}")
      print(f"  底层原因: {failure.error.__cause__!r}")
  ```

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

## 输出根语义（多清单场景必读）

清单内文件路径相对 Riot 原生安装根（LCU 与 `Game\` 同级）。
**输出根 = 该清单对应产品的安装根**：不同清单不可共用同一输出根。
例如腾讯服将 LCU 挪进 `LeagueClient\` 子目录，因此 LCU 清单必须以
`<游戏根>\LeagueClient` 为输出根同步，GAME 清单以游戏根为输出根；
共用同一输出根会把 LCU 文件写进游戏根（凭空多出 `Plugins\` 等目录）。

## 下载日志

正常运行 INFO 只有批次级摘要；DEBUG 级别每个 bundle 作业成功会输出
`bundle_id、bytes、elapsed、speed、retries`，例如：

```
bundle作业完成: 00000000075BCA5C, bytes=4194304, elapsed=1.20s, speed=3495253B/s, retries=0
```

使用侧可用日志路由（如 loguru 按 `record["name"].startswith("riotmanifest")`
过滤）把 DEBUG 传输记录收进独立下载日志文件，用于慢速 / 波动问题的事后分析。

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

## 致谢

本模块的验证与同步语义（chunk 级固定位置验证、hash_type 穷举猜测、
`--update` / `--no-write` 等模式对应关系）参考了
[moonshadow565/rman](https://github.com/moonshadow565/rman) 的实现。
