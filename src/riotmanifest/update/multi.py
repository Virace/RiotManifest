"""多清单联合同步入口：单 worker 池、单进度流."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from riotmanifest.update.result import SyncMode, UpdateResult
from riotmanifest.update.updater import ManifestUpdater, _sync_updaters

if TYPE_CHECKING:
    from riotmanifest.diff.manifest_diff import ManifestInput
    from riotmanifest.downloader.scheduler import ProgressCallback
    from riotmanifest.manifest import PatcherFile, PatcherManifest


@dataclass(slots=True)
class SyncTarget:
    """`sync_many` 的单清单同步目标.

    Attributes:
        manifest: 目标清单；其 `path` 为该清单的输出根。输出根应为该清单
            对应产品的安装根，不同清单不可共用同一输出根。
        files: 目标文件列表；None 表示清单全部文件。
        old_manifest: 旧清单（路径 / URL / 实例）；None 时行为同 `ManifestUpdater`。
        archive: 是否在输出根维护 `.rman/` 存档与 installed.json。
    """

    manifest: PatcherManifest
    files: Iterable[PatcherFile] | None = None
    old_manifest: ManifestInput | None = None
    archive: bool = True


async def sync_many(
    targets: Sequence[SyncTarget],
    *,
    mode: SyncMode = SyncMode.AUTO,
    remove_deleted: bool = True,
    concurrency_limit: int | None = None,
    progress_callback: ProgressCallback | None = None,
    progress_interval_seconds: float | None = 1.0,
) -> list[UpdateResult]:
    """对多个清单执行联合同步：合并下载调度，进度跨清单合计.

    各清单保留自己的输出根、旧清单解析与 `.rman` 存档语义，仅合并
    bundle 下载调度：单 worker 池（`concurrency_limit` 为全局并发，
    缺省取各清单 `concurrency_limit` 的最大值）、单一进度事件流
    （`DownloadProgress` 的 total/finished 为跨清单合计，按字节加权天然准确）。

    Args:
        targets: 同步目标列表。
        mode: 同步模式，作用于全部目标。
        remove_deleted: 是否删除旧清单声明、新清单不含的文件。
        concurrency_limit: 全局下载并发 worker 数。
        progress_callback: 进度回调，接收跨清单合计的进度事件。
        progress_interval_seconds: 进度周期上报间隔（秒）。

    Returns:
        与 `targets` 顺序一致的 `UpdateResult` 列表；部分失败不抛异常。
    """
    if not targets:
        return []
    updaters = [
        ManifestUpdater(target.manifest, old_manifest=target.old_manifest, archive=target.archive)
        for target in targets
    ]
    files_list = [
        list(target.files) if target.files is not None else list(target.manifest.files.values())
        for target in targets
    ]
    return await _sync_updaters(
        updaters,
        files_list,
        mode=mode,
        remove_deleted=remove_deleted,
        concurrency_limit=concurrency_limit,
        progress_callback=progress_callback,
        progress_interval_seconds=progress_interval_seconds,
    )
