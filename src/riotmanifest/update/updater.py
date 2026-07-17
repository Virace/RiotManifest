"""增量更新编排器.

统一"下载 = 修复 = 更新"三态语义：

- 目标文件不存在 → 全量下载；
- 目标文件存在 → chunk 级固定位置验证 + 补洞；
- 有旧清单（显式传入或 installed.json 存档）→ 文件级跳过未变化文件。

写盘一律走 staging 临时文件 + 原子替换；部分失败不推进 installed.json。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable
from typing import TYPE_CHECKING, Union

from riotmanifest.diff.manifest_diff import ManifestInput, _ensure_manifest, diff_manifests
from riotmanifest.downloader.scheduler import ChunkEntry, iter_chunk_entries
from riotmanifest.downloader.staging import commit_staging, discard_staging, staging_path
from riotmanifest.manifest import PatcherManifest
from riotmanifest.update.planner import FileAction, build_update_plan
from riotmanifest.update.result import SyncMode, UpdateResult
from riotmanifest.update.state import ManifestArchive
from riotmanifest.update.verify import verify_file_chunks

if TYPE_CHECKING:
    from riotmanifest.downloader.scheduler import ProgressCallback
    from riotmanifest.manifest import PatcherFile

StrPath = Union[str, "os.PathLike[str]"]


class ManifestUpdater:
    """基于新旧清单 diff 与本地验证的增量更新编排器."""

    def __init__(self, manifest: PatcherManifest, old_manifest: ManifestInput | None = None):
        """初始化编排器.

        Args:
            manifest: 目标（新）清单，其 `path` 即输出目录。
            old_manifest: 旧清单（路径/URL/实例）；None 时自动从
                输出目录的 installed.json 存档解析。
        """
        self.manifest = manifest
        self.old_manifest = old_manifest
        self.archive = ManifestArchive(manifest.path)

    def _resolve_old_manifest(self) -> PatcherManifest | None:
        """按 显式传入 > installed.json 存档 > 无 的顺序解析旧清单."""
        if self.old_manifest is not None:
            return _ensure_manifest(self.old_manifest)
        archived = self.archive.installed_manifest_path()
        if archived is not None:
            return PatcherManifest(str(archived), path="")
        return None

    @staticmethod
    def _copy_hits(source: str, output: StrPath, hits: list[ChunkEntry]) -> None:
        """把验证命中的块从本地源文件复制到目标的 staging 对应偏移."""
        staging = staging_path(output)
        with open(source, "rb") as src, open(staging, "r+b") as dst:
            for entry in hits:
                src.seek(entry.file_offset)
                data = src.read(entry.chunk.target_size)
                dst.seek(entry.file_offset)
                dst.write(data)

    async def sync(
        self,
        files: Iterable[PatcherFile] | None = None,
        *,
        mode: SyncMode = SyncMode.AUTO,
        remove_deleted: bool = True,
        concurrency_limit: int | None = None,
        progress_callback: ProgressCallback | None = None,
        progress_interval_seconds: float | None = 1.0,
    ) -> UpdateResult:
        """执行同步（下载 / 修复 / 更新统一入口）.

        Args:
            files: 目标文件列表（如 `filter_files` 的结果）；None 表示新清单全部文件。
            mode: 同步模式，见 `SyncMode`。
            remove_deleted: 是否删除旧清单声明、新清单不含的文件（含 MOVE 的旧路径）。
            concurrency_limit: 下载并发 worker 数。
            progress_callback: 下载进度回调（透传给调度器）。
            progress_interval_seconds: 进度周期上报间隔（秒）。

        Returns:
            结构化同步结果；部分失败不抛异常，通过 `failed` 反馈且不推进 installed.json。
        """
        manifest = self.manifest
        target_files = list(files) if files is not None else list(manifest.files.values())
        verify_only = mode is SyncMode.VERIFY_ONLY

        report = None
        if mode is not SyncMode.FORCE_FULL:
            old = self._resolve_old_manifest()
            if old is not None:
                report = diff_manifests(old, manifest, include_unchanged=True, detect_moves=True)
        plan = build_update_plan(report, target_files)

        actions = {entry.path: entry.action for entry in plan.entries}
        downloaded_bytes = 0
        reused_bytes = 0
        missing_bytes = 0
        staged: list[tuple[PatcherFile, str]] = []
        misses: list[ChunkEntry] = []

        for entry in plan.entries:
            if entry.action in (FileAction.SKIP, FileAction.REMOVE):
                continue
            file = entry.file
            assert file is not None  # nosec: B101 - 非 REMOVE 条目必有文件对象
            output = manifest.file_output(file)

            if mode is SyncMode.FORCE_FULL:
                manifest.preallocate_file(file)
                staged.append((file, output))
                misses.extend(iter_chunk_entries(file))
                continue

            # MOVE 从旧路径读取，其余动作以目标路径自身为本地数据来源。
            source = output if entry.move_from is None else os.path.join(manifest.path, entry.move_from)
            result = await asyncio.to_thread(verify_file_chunks, file, source)
            reused_bytes += result.reused_bytes

            if verify_only:
                missing_bytes += sum(miss.chunk.target_size for miss in result.misses)
                continue

            if entry.action is FileAction.PATCH and result.complete:
                # 本地已与清单一致，零写盘。
                continue

            manifest.preallocate_file(file)
            staged.append((file, output))
            if result.hits:
                await asyncio.to_thread(self._copy_hits, source, output, result.hits)
            misses.extend(result.misses)

        if verify_only:
            return UpdateResult(
                actions=actions,
                reused_bytes=reused_bytes,
                missing_bytes=missing_bytes,
                verify_only=True,
            )

        failed_paths: set[str] = set()
        if misses:
            failed_paths = await manifest.downloader.download_chunk_entries(
                misses,
                concurrency_limit=concurrency_limit,
                raise_on_error=False,
                progress_callback=progress_callback,
                progress_interval_seconds=progress_interval_seconds,
                manage_staging=False,
            )
            downloaded_bytes = sum(miss.chunk.target_size for miss in misses if miss.file.name not in failed_paths)
            missing_bytes = sum(miss.chunk.target_size for miss in misses if miss.file.name in failed_paths)

        def _finalize() -> None:
            for file, output in staged:
                if file.name in failed_paths:
                    discard_staging(output)
                else:
                    commit_staging(output)

        await asyncio.to_thread(_finalize)

        removed: list[str] = []
        if remove_deleted:

            def _cleanup() -> None:
                for plan_entry in plan.by_action(FileAction.REMOVE):
                    target = os.path.join(manifest.path, plan_entry.path)
                    if os.path.isfile(target):
                        os.remove(target)
                        removed.append(plan_entry.path)
                for plan_entry in plan.by_action(FileAction.MOVE):
                    if plan_entry.move_from is None or plan_entry.path in failed_paths:
                        continue
                    source = os.path.join(manifest.path, plan_entry.move_from)
                    if os.path.isfile(source):
                        os.remove(source)

            await asyncio.to_thread(_cleanup)

        # 版本指针只在整批成功后推进，避免"半新不旧"状态被记录为已安装。
        if not failed_paths and manifest.raw_bytes is not None:
            await asyncio.to_thread(
                self.archive.save,
                manifest.manifest_id,
                manifest.raw_bytes,
                str(manifest.file),
            )

        return UpdateResult(
            actions=actions,
            downloaded_bytes=downloaded_bytes,
            reused_bytes=reused_bytes,
            missing_bytes=missing_bytes,
            removed=removed,
            failed=sorted(failed_paths),
            verify_only=False,
        )
