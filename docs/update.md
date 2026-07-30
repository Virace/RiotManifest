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

`PatcherManifest` 的下载接口与 `ManifestUpdater` 的受管理安装是两种意图：

- `PatcherManifest.download_files_concurrently()` / `PatcherFile.download_file()`：
  单独下载，不读取或写入安装状态，也没有移动、删除权限。
- `ManifestUpdater`：受管理安装，默认维护 schema 2 状态并据此执行增量动作。

受管理安装的文件处理仍复用同一条验证/下载管线：

| 本地状态 | 行为 |
|---|---|
| 目标文件不存在 | 全量下载 |
| 目标文件存在 | 按新清单 chunk 布局逐块验证，命中保留、miss 补洞下载 |
| 有旧清单，但文件不在 schema 2 覆盖中 | 旧清单只作 diff 提示；目标仍逐 chunk 验证 |
| 文件未变化、在 schema 2 覆盖中，且磁盘类型/大小正确 | 整个文件快速跳过 |

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

首次运行没有可信覆盖时自动退化为"逐文件验证 + 缺口下载"；
成功后清单和本次确认的文件集合会写入 schema 2。后续对同一清单同步
其他文件时会累积覆盖，不会把一次部分同步误记为完整安装。

## `ManifestUpdater`

构造参数：

- `manifest`：目标（新）清单，其 `path` 即输出目录。
- `old_manifest`：旧清单，支持路径 / URL / `PatcherManifest` 实例。
  不传时自动从输出目录的 `installed.json` 存档解析。显式旧清单本身
  只是 diff 提示，不能授权跳过、移动或删除；这些动作还需要匹配的
  schema 2 `files` 所有权。
- `archive`（keyword-only，默认 `True`）：是否在输出目录维护 `.rman/` 存档
  与 installed.json。`False` 时完全不读取或写入安装状态；旧清单只认显式
  传入，且不授予跳过、移动或删除权限。适合输出目录不归本库管辖的场景。

### `sync()`

- `files`：目标文件列表（如 `filter_files` 的结果）；`None` 表示全部文件。
- `mode`：见下方 `SyncMode`。
- `remove_deleted`：是否删除旧清单声明、新清单不含的文件（含移动后的旧路径），默认 `True`。
  只会删除 schema 2 `files` 明确管理、且新清单不再需要的路径，不碰目录里的
  陌生文件。`False` 会保留旧文件，但新状态不再声明其所有权。
- `concurrency_limit` / `progress_callback` / `progress_interval_seconds`：透传给下载调度器。

返回 `UpdateResult`。部分文件失败不抛异常：失败文件记入 `failed`，
失败原因（bundle_id + 原始异常）记入 `failures`，
其旧文件保持原样，本次不执行清理，也不更新 `installed.json`
（版本指针只在整批成功后推进）。

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
  内容，只检查 schema 2 所有权、普通文件类型和大小。它能发现缺失/截断，
  但不能发现同大小损坏；怀疑内容损坏时用 `REPAIR`。
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

- `verify_only`：本次是否为 `VERIFY_ONLY` 运行。
- `committed_files`：经 staging 成功提交到目标路径的清单文件名列表。
  只统计本轮真实写盘的文件：验证完全命中的 PATCH、REMOVE 和下载失败文件不计入；
  MOVE、全新下载及纯本地 chunk 命中重建只要最终提交成功就计入。同一路径最多
  出现一次，顺序与计划内文件顺序一致；`VERIFY_ONLY` 恒为空列表。
  `actions` 仍是计划口径；需要统计实际更新文件时应使用本字段。
  `sync_many` 返回的每个结果只包含所属 target 的提交路径。

## 本地状态与磁盘布局

更新依赖两类本地信息，均不使用数据库：

```
<output>/.rman/installed.json              当前安装状态指针
<output>/.rman/manifests/<ID>.manifest     清单原文存档（保留当前 + 上一份）
<output>/<file>.rman-tmp                   下载过程中的临时文件（staging）
```

- 所有写入先落 staging，文件全部 chunk 就绪后原子替换到目标路径；
  中断或失败时旧文件保持完整。峰值磁盘占用约多出"单个在写文件"的大小。
- `installed.json` 的 schema 2 与 Go 项目 RiotManifestGo 共享，
  两个工具指向同一个安装根时状态可互认。`files` 是排序、去重、统一使用
  `/` 的 manifest 相对路径，表示当前存档清单下已确认的受管理文件：

  ```json
  {
    "schema": 2,
    "manifest_id": "037EC59D5BD7C5D3",
    "manifest_file": "manifests/037EC59D5BD7C5D3.manifest",
    "source": "https://example.invalid/releases/037EC59D5BD7C5D3.manifest",
    "updated_at": "2026-07-29T12:00:00Z",
    "files": [
      "Config/description.json",
      "DATA/FINAL/Maps/Shipping.wad.client"
    ]
  }
  ```

- schema 1 没有文件覆盖证据：仍可读取其旧清单作 diff 提示，但不会授权
  `SKIP`、`MOVE` 或 `REMOVE`；下一次成功的受管理同步会写成 schema 2。
- 跨版本时只继承内容未变化、仍在新清单中、且通过类型/大小门禁的受管理文件；
  changed 但本轮未选择的旧文件会留在磁盘，却不会被新状态误认为已安装。

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
（unchanged/changed/added/moved/removed → 跳过/补丁/新下/改名/删除），
但 diff 只说明清单变化，schema 2 所有权和磁盘门禁才决定是否允许受管理动作。
如果你同时需要"资源侧更新日志"，可以自己再调一次 `diff_manifests`
并使用其 JSON 导出，两者结论一致。

## 行为变更说明（相对 2.x 下载语义）

- `download_files_concurrently` 不再按"文件大小相同"跳过已存在文件，
  语义为无状态的"给什么下什么"，不接触 `.rman`；需要受管理增量语义时
  使用 `updater.sync()`。
- 下载写盘改为 staging + 原子替换：中断不再损坏已有文件。

## 致谢

本模块的验证与同步语义（chunk 级固定位置验证、hash_type 穷举猜测、
`--update` / `--no-write` 等模式对应关系）参考了
[moonshadow565/rman](https://github.com/moonshadow565/rman) 的实现。
