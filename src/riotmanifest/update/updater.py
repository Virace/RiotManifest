"""增量更新编排器.

统一"下载 = 修复 = 更新"三态语义：

- 目标文件不存在 → 全量下载；
- 目标文件存在 → chunk 级固定位置验证 + 补洞；
- 有匹配的 schema 2 安装状态 → 对受管理且通过磁盘门禁的未变化文件跳过。

写盘一律走 staging 临时文件 + 原子替换；部分失败不推进 installed.json。

进度事件覆盖完整 sync 生命周期（phase 语义详见 docs/update.md）：
verify（周期 + 结束快照）→ plan_ready（下载总量，哪怕为 0）→
下载阶段事件（调度层产生）→ finalize → sync_completed / sync_failed。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Union

from riotmanifest.diff.manifest_diff import (
    ManifestDiffReport,
    ManifestInput,
    _ensure_manifest,
    diff_manifests,
)
from riotmanifest.downloader.scheduler import (
    ChunkEntry,
    DownloadProgress,
    DownloadScheduler,
    JobGroup,
    iter_chunk_entries,
    run_job_groups,
)
from riotmanifest.downloader.staging import commit_staging, discard_staging, staging_path
from riotmanifest.manifest import PatcherManifest
from riotmanifest.update.planner import (
    FileAction,
    UpdatePlan,
    _is_regular_size,
    build_update_plan,
)
from riotmanifest.update.result import SyncMode, UpdateResult
from riotmanifest.update.state import STATE_SCHEMA, ManifestArchive
from riotmanifest.update.verify import verify_file_chunks

if TYPE_CHECKING:
    from riotmanifest.core.errors import BundleJobFailure
    from riotmanifest.downloader.scheduler import ProgressCallback
    from riotmanifest.manifest import PatcherFile

StrPath = Union[str, "os.PathLike[str]"]


class _VerifyReporter:
    """verify 阶段的进度计数与周期上报（供单/多清单同步共享）.

    计数语义：`total_bytes` 为本轮所有待验证文件的解压域大小合计；
    `verified_bytes` 由逐 chunk 钩子推进，并在每个文件完成后对齐到
    "已完成文件大小合计"——文件不存在等快速路径因此不会丢进度。
    验证逐文件串行执行（同一时刻只有一个工作线程调用钩子），无并发写。
    """

    def __init__(
        self,
        total_bytes: int,
        total_files: int,
        progress_callback: ProgressCallback | None,
        interval_seconds: float | None,
    ):
        self.total_bytes = total_bytes
        self.total_files = total_files
        self.progress_callback = progress_callback
        interval_enabled = interval_seconds is not None and interval_seconds > 0
        self.interval_seconds = interval_seconds if interval_enabled else None
        self.verified_bytes = 0
        self.finished_files = 0
        self.start_time = time.perf_counter()
        self._finished_bytes_base = 0
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        """是否需要产生 verify 阶段事件."""
        return self.progress_callback is not None and self.total_files > 0

    def chunk_hook(self, nbytes: int) -> None:
        """逐 chunk 验证钩子（在验证工作线程内调用）."""
        self.verified_bytes += nbytes

    def file_done(self, file_size: int) -> None:
        """单文件验证完成，把计数对齐到已完成文件合计."""
        self._finished_bytes_base += file_size
        self.verified_bytes = self._finished_bytes_base
        self.finished_files += 1

    def snapshot(self) -> DownloadProgress:
        """构建 verify 阶段进度快照."""
        elapsed = max(time.perf_counter() - self.start_time, 0.0)
        done = min(self.verified_bytes, self.total_bytes)
        return DownloadProgress(
            phase="verify",
            total_jobs=self.total_files,
            finished_jobs=self.finished_files,
            succeeded_jobs=self.finished_files,
            failed_jobs=0,
            total_bytes=self.total_bytes,
            finished_bytes=done,
            progress=done / self.total_bytes if self.total_bytes else 1.0,
            elapsed_seconds=elapsed,
            average_speed_bytes_per_sec=done / elapsed if elapsed > 0 else 0.0,
        )

    async def start(self) -> None:
        """启动周期上报任务（无回调或无间隔时为空操作）."""
        if not self.enabled or self.interval_seconds is None:
            return

        async def _tick() -> None:
            # 等停止事件带超时而非裸 sleep：stop() 不必等满一个间隔即可返回。
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
                    break
                except asyncio.TimeoutError:  # noqa: UP041 - 3.10 下两者不同类
                    pass
                await DownloadScheduler.emit_progress(self.progress_callback, self.snapshot())

        self._task = asyncio.create_task(_tick())

    async def stop(self) -> None:
        """停止周期上报并补发结束快照（保证 verify 阶段至少一个事件）."""
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None
        if self.enabled:
            await DownloadScheduler.emit_progress(self.progress_callback, self.snapshot())


@dataclass(slots=True)
class _SyncPrep:
    """单清单 plan + verify 阶段产物，供下载与收尾阶段消费."""

    plan: UpdatePlan
    actions: dict[str, FileAction]
    staged: list[tuple[PatcherFile, str]]
    misses: list[ChunkEntry]
    reused_bytes: int
    missing_bytes: int
    next_files: set[str]


@dataclass(slots=True)
class _InstallBase:
    """旧清单与其可信受管理文件集合；显式旧清单本身不授予所有权."""

    old: PatcherManifest | None
    files: set[str]


@dataclass(slots=True)
class _SyncPlan:
    """单清单计划及成功后可写入 schema 2 的文件覆盖."""

    plan: UpdatePlan
    next_files: set[str]


def _verify_scope(plan: UpdatePlan, mode: SyncMode) -> tuple[int, int]:
    """返回 verify 阶段的 (总字节数, 文件数)；FORCE_FULL 无验证阶段."""
    if mode is SyncMode.FORCE_FULL:
        return 0, 0
    total_bytes = 0
    total_files = 0
    for entry in plan.entries:
        if entry.action in (FileAction.SKIP, FileAction.REMOVE):
            continue
        assert entry.file is not None  # nosec: B101 - 非 REMOVE 条目必有文件对象
        total_bytes += entry.file.size
        total_files += 1
    return total_bytes, total_files


async def _emit_phase(
    progress_callback: ProgressCallback | None,
    phase: str,
    *,
    total_jobs: int,
    total_bytes: int,
    start_time: float,
    finished: bool = False,
) -> None:
    """发出编排层里程碑事件（plan_ready / finalize / sync_completed / sync_failed）.

    total 口径与下载阶段一致（bundle 作业数 / 压缩域字节数）；
    `finished=True` 表示下载量已全部结清（或本就无下载）。
    """
    if progress_callback is None:
        return
    elapsed = max(time.perf_counter() - start_time, 0.0)
    finished_jobs = total_jobs if finished else 0
    finished_bytes = total_bytes if finished else 0
    await DownloadScheduler.emit_progress(
        progress_callback,
        DownloadProgress(
            phase=phase,
            total_jobs=total_jobs,
            finished_jobs=finished_jobs,
            succeeded_jobs=finished_jobs,
            failed_jobs=0,
            total_bytes=total_bytes,
            finished_bytes=finished_bytes,
            progress=1.0 if finished or total_jobs == 0 else 0.0,
            elapsed_seconds=elapsed,
            average_speed_bytes_per_sec=0.0,
        ),
    )


class ManifestUpdater:
    """基于新旧清单 diff 与本地验证的增量更新编排器."""

    def __init__(
        self,
        manifest: PatcherManifest,
        old_manifest: ManifestInput | None = None,
        *,
        archive: bool = True,
    ):
        """初始化编排器.

        Args:
            manifest: 目标（新）清单，其 `path` 即输出目录。
            old_manifest: 旧清单（路径/URL/实例）；None 时自动从
                输出目录的 installed.json 存档解析。
            archive: 是否在输出目录维护 `.rman/` 存档与 installed.json。
                False 时不创建存档、不推进版本指针，旧清单只认显式传入
                （适合输出目录不归本库管辖的场景，如玩家游戏目录）。
        """
        self.manifest = manifest
        self.old_manifest = old_manifest
        self.archive = ManifestArchive(manifest.path) if archive else None

    def _resolve_base(self) -> _InstallBase:
        """解析 diff 基底，并仅在 schema 2 状态与旧清单匹配时授予所有权."""
        state = self.archive.load_installed() if self.archive is not None else None
        archived = self.archive.installed_manifest_path() if self.archive is not None else None
        explicit = _ensure_manifest(self.old_manifest) if self.old_manifest is not None else None
        old = explicit
        files: set[str] = set()

        if state is not None and archived is not None and state.schema == STATE_SCHEMA:
            state_id = state.manifest_id.upper()
            if explicit is not None and state_id == f"{explicit.manifest_id:016X}":
                managed_old = explicit
            elif state_id == f"{self.manifest.manifest_id:016X}":
                managed_old = self.manifest
            else:
                managed_old = PatcherManifest(str(archived), path="")

            if state_id == f"{managed_old.manifest_id:016X}":
                old = managed_old
                files.update(state.files)

        if old is None and archived is not None:
            old = PatcherManifest(str(archived), path="")
        return _InstallBase(old=old, files=files)

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

    def _build_plan(self, target_files: list[PatcherFile], mode: SyncMode) -> _SyncPlan:
        """构建动作计划与成功状态；仅 AUTO 复用文件，受管理清理独立于策略."""
        base = self._resolve_base()
        report = None
        if base.old is not None and mode is not SyncMode.VERIFY_ONLY:
            # strict：哈希类型跨版本迁移（如 16.3 HKDF → 16.4 BLAKE3）时，
            # loose 会跳过 chunk 比较，大小恰好相同的变更文件会被误判 unchanged。
            report = diff_manifests(
                base.old,
                self.manifest,
                include_unchanged=True,
                detect_moves=True,
                hash_type_mismatch_mode="strict",
            )

        plan = build_update_plan(
            report,
            target_files,
            managed_files=base.files,
            output_dir=self.manifest.path,
            allow_reuse=mode is SyncMode.AUTO,
        )
        next_files = self._carried_files(report, base.files)
        next_files.update(file.name for file in target_files if not file.link)
        return _SyncPlan(plan=plan, next_files=next_files)

    def _carried_files(
        self,
        report: ManifestDiffReport | None,
        managed_files: set[str],
    ) -> set[str]:
        """保留跨版本未变化、仍存在且通过普通文件/大小门禁的受管理路径."""
        if report is None or not managed_files:
            return set()

        unchanged = {entry.path for entry in report.unchanged}
        carried: set[str] = set()
        for path in managed_files & unchanged:
            file = self.manifest.files.get(path)
            if file is None or file.link:
                continue
            if _is_regular_size(self.manifest.path, path, file.size):
                carried.add(path)
        return carried

    async def _verify_plan(self, sync_plan: _SyncPlan, mode: SyncMode, reporter: _VerifyReporter) -> _SyncPrep:
        """对计划内文件做本地验证，产出 staging 与待下载 miss 列表.

        VERIFY_ONLY 只统计缺口与 miss 列表（供计算下载总量），
        不预分配、不写盘。
        """
        manifest = self.manifest
        plan = sync_plan.plan
        verify_only = mode is SyncMode.VERIFY_ONLY
        actions = {entry.path: entry.action for entry in plan.entries}
        staged: list[tuple[PatcherFile, str]] = []
        misses: list[ChunkEntry] = []
        reused_bytes = 0
        missing_bytes = 0

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
            result = await asyncio.to_thread(verify_file_chunks, file, source, reporter.chunk_hook)
            reporter.file_done(file.size)
            reused_bytes += result.reused_bytes

            if verify_only:
                missing_bytes += sum(miss.chunk.target_size for miss in result.misses)
                misses.extend(result.misses)
                continue

            if entry.action is FileAction.PATCH and result.complete:
                # 本地已与清单一致，零写盘。
                continue

            manifest.preallocate_file(file)
            staged.append((file, output))
            if result.hits:
                await asyncio.to_thread(self._copy_hits, source, output, result.hits)
            misses.extend(result.misses)

        return _SyncPrep(
            plan=plan,
            actions=actions,
            staged=staged,
            misses=misses,
            reused_bytes=reused_bytes,
            missing_bytes=missing_bytes,
            next_files=sync_plan.next_files,
        )

    async def _finish(
        self,
        prep: _SyncPrep,
        *,
        remove_deleted: bool,
        failed_paths: set[str],
        failures: list[BundleJobFailure],
    ) -> UpdateResult:
        """收尾：staging 提交/丢弃、清理删除项、推进存档并构建结果."""
        manifest = self.manifest
        downloaded_bytes = sum(miss.chunk.target_size for miss in prep.misses if miss.file.name not in failed_paths)
        missing_bytes = sum(miss.chunk.target_size for miss in prep.misses if miss.file.name in failed_paths)

        def _finalize() -> None:
            for file, output in prep.staged:
                if file.name in failed_paths:
                    discard_staging(output)
                else:
                    commit_staging(output)

        await asyncio.to_thread(_finalize)

        removed: list[str] = []
        if remove_deleted and not failed_paths:

            def _cleanup() -> None:
                for plan_entry in prep.plan.by_action(FileAction.REMOVE):
                    target = os.path.join(manifest.path, plan_entry.path)
                    if os.path.isfile(target):
                        os.remove(target)
                        removed.append(plan_entry.path)
                for plan_entry in prep.plan.by_action(FileAction.MOVE):
                    if plan_entry.move_from is None or plan_entry.path in failed_paths:
                        continue
                    source = os.path.join(manifest.path, plan_entry.move_from)
                    if os.path.isfile(source):
                        os.remove(source)

            await asyncio.to_thread(_cleanup)

        # 版本指针只在整批成功后推进，避免"半新不旧"状态被记录为已安装。
        if not failed_paths and self.archive is not None and manifest.raw_bytes is not None:
            await asyncio.to_thread(
                self.archive.save,
                manifest.manifest_id,
                manifest.raw_bytes,
                str(manifest.file),
                prep.next_files,
            )

        return UpdateResult(
            actions=prep.actions,
            downloaded_bytes=downloaded_bytes,
            reused_bytes=prep.reused_bytes,
            missing_bytes=missing_bytes,
            removed=removed,
            failed=sorted(failed_paths),
            failures=failures,
            verify_only=False,
        )

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
            progress_callback: 进度回调，覆盖完整 sync 生命周期
                （verify / plan_ready / 下载阶段 / finalize / 终态）。
            progress_interval_seconds: 进度周期上报间隔（秒）。

        Returns:
            结构化同步结果；部分失败不抛异常，通过 `failed` 反馈且不推进 installed.json。
        """
        target_files = list(files) if files is not None else list(self.manifest.files.values())
        results = await _sync_updaters(
            [self],
            [target_files],
            mode=mode,
            remove_deleted=remove_deleted,
            concurrency_limit=concurrency_limit,
            progress_callback=progress_callback,
            progress_interval_seconds=progress_interval_seconds,
        )
        return results[0]


async def _sync_updaters(
    updaters: list[ManifestUpdater],
    files_list: list[list[PatcherFile]],
    *,
    mode: SyncMode,
    remove_deleted: bool,
    concurrency_limit: int | None,
    progress_callback: ProgressCallback | None,
    progress_interval_seconds: float | None,
) -> list[UpdateResult]:
    """多 updater 联合同步的共享编排核心：单 worker 池、单进度流.

    各清单保留自己的输出根与存档语义，仅合并 bundle 下载调度；
    进度事件的 total/finished 为所有清单合计（字节加权天然准确）。
    返回与入参顺序一致的结果列表。
    """
    verify_only = mode is SyncMode.VERIFY_ONLY

    sync_plans = [updater._build_plan(target_files, mode) for updater, target_files in zip(updaters, files_list, strict=True)]
    total_verify_bytes = 0
    total_verify_files = 0
    for sync_plan in sync_plans:
        plan_bytes, plan_files = _verify_scope(sync_plan.plan, mode)
        total_verify_bytes += plan_bytes
        total_verify_files += plan_files

    reporter = _VerifyReporter(total_verify_bytes, total_verify_files, progress_callback, progress_interval_seconds)
    await reporter.start()
    try:
        preps = [
            await updater._verify_plan(sync_plan, mode, reporter) for updater, sync_plan in zip(updaters, sync_plans, strict=True)
        ]
    finally:
        await reporter.stop()

    # plan_ready 与下载阶段完全同口径：此处即构建最终 bundle 作业。
    groups: list[JobGroup] = []
    group_updater_indexes: list[int] = []
    for updater_index, (updater, prep) in enumerate(zip(updaters, preps, strict=True)):
        if not prep.misses:
            continue
        jobs = updater.manifest.downloader.build_bundle_jobs([], entries=prep.misses)
        if not jobs:
            continue
        groups.append(JobGroup(scheduler=updater.manifest.downloader, jobs=jobs))
        group_updater_indexes.append(updater_index)

    plan_total_jobs = sum(len(group.jobs) for group in groups)
    plan_total_bytes = sum(DownloadScheduler.job_total_bytes(job) for group in groups for job in group.jobs)
    start_time = time.perf_counter()
    await _emit_phase(
        progress_callback,
        "plan_ready",
        total_jobs=plan_total_jobs,
        total_bytes=plan_total_bytes,
        start_time=start_time,
    )

    if verify_only:
        results = [
            UpdateResult(
                actions=prep.actions,
                reused_bytes=prep.reused_bytes,
                missing_bytes=prep.missing_bytes,
                verify_only=True,
            )
            for prep in preps
        ]
        await _emit_phase(
            progress_callback,
            "sync_completed",
            total_jobs=plan_total_jobs,
            total_bytes=plan_total_bytes,
            start_time=start_time,
            finished=True,
        )
        return results

    failures_per_updater: list[list[BundleJobFailure]] = [[] for _ in updaters]
    if groups:
        grouped_failures = await run_job_groups(
            groups,
            concurrency_limit=concurrency_limit,
            progress_callback=progress_callback,
            progress_interval_seconds=progress_interval_seconds,
        )
        for updater_index, group_failures in zip(group_updater_indexes, grouped_failures, strict=True):
            failures_per_updater[updater_index] = group_failures

    await _emit_phase(
        progress_callback,
        "finalize",
        total_jobs=plan_total_jobs,
        total_bytes=plan_total_bytes,
        start_time=start_time,
        finished=True,
    )

    results: list[UpdateResult] = []
    any_failed = False
    for updater, prep, failures in zip(updaters, preps, failures_per_updater, strict=True):
        failed_bundle_ids = {failure.bundle_id for failure in failures}
        failed_paths = {entry.file.name for entry in prep.misses if entry.chunk.bundle.bundle_id in failed_bundle_ids}
        any_failed = any_failed or bool(failed_paths)
        results.append(
            await updater._finish(
                prep,
                remove_deleted=remove_deleted,
                failed_paths=failed_paths,
                failures=failures,
            )
        )

    await _emit_phase(
        progress_callback,
        "sync_failed" if any_failed else "sync_completed",
        total_jobs=plan_total_jobs,
        total_bytes=plan_total_bytes,
        start_time=start_time,
        finished=True,
    )
    return results
