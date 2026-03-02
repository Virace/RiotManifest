"""Manifest 下载调度实现.

该模块聚焦并发下载、Range 请求拼接、解压与写盘流程，
避免 `PatcherManifest` 同时承担“解析 + 下载调度”两类复杂职责。
"""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urljoin

import aiohttp
import pyzstd
from loguru import logger

from riotmanifest.core.errors import BundleJobFailure, DecompressError, DownloadBatchError, DownloadError
from riotmanifest.downloader.file_pool import FileHandlePool


@dataclass(frozen=True)
class DownloadProgress:
    """下载进度与速度快照."""

    phase: str
    total_jobs: int
    finished_jobs: int
    succeeded_jobs: int
    failed_jobs: int
    total_bytes: int
    finished_bytes: int
    progress: float
    elapsed_seconds: float
    average_speed_bytes_per_sec: float
    bundle_id: int | None = None


ProgressCallback = Callable[[DownloadProgress], Awaitable[None] | None]


@dataclass
class WriteTarget:
    """单个 chunk 的文件写入目标."""

    file: PatcherFile
    file_offset: int
    expected_len: int
    chunk_id: int
    hash_type: int


@dataclass
class GlobalChunkTask:
    """全局去重后的 chunk 下载任务."""

    chunk: PatcherChunk
    targets: list[WriteTarget] = field(default_factory=list)


@dataclass
class ChunkRange:
    """Bundle 内单段 Range 请求定义."""

    start: int
    end: int
    tasks: list[GlobalChunkTask] = field(default_factory=list)


@dataclass
class BundleJob:
    """Bundle 下载任务."""

    bundle_id: int
    ranges: list[ChunkRange] = field(default_factory=list)


class DownloadScheduler:
    """Manifest 下载调度器.

    该类不直接持有文件与索引数据，而是依赖 `PatcherManifest` 提供
    元数据、配置与少量回调（哈希校验、文件路径解析等）。
    """

    CONTENT_RANGE_REGEX = re.compile(r"^bytes\s+(\d+)-(\d+)/(?:\d+|\*)$", re.I)

    def __init__(self, manifest: PatcherManifest):
        """初始化下载调度器.

        Args:
            manifest: 拥有索引数据与配置的 Manifest 对象。
        """
        self.manifest = manifest

    def build_global_task_map(self, files: list[PatcherFile]) -> dict[int, list[GlobalChunkTask]]:
        """按 ChunkID 去重并构建全局任务映射.

        Args:
            files: 需要下载的目标文件列表。

        Returns:
            以 bundle_id 分组的任务映射。
        """
        chunk_index: dict[int, GlobalChunkTask] = {}

        for file in files:
            file_offset = 0
            # 一个文件由多个 chunk 拼接，记录每个 chunk 在目标文件中的写入偏移。
            for chunk in file.chunks:
                target = WriteTarget(
                    file=file,
                    file_offset=file_offset,
                    expected_len=chunk.target_size,
                    chunk_id=chunk.chunk_id,
                    hash_type=file.chunk_hash_types.get(chunk.chunk_id, 0),
                )
                if chunk.chunk_id in chunk_index:
                    chunk_index[chunk.chunk_id].targets.append(target)
                else:
                    chunk_index[chunk.chunk_id] = GlobalChunkTask(chunk=chunk, targets=[target])
                file_offset += chunk.target_size

        bundle_map: dict[int, list[GlobalChunkTask]] = {}
        for task in chunk_index.values():
            bundle_id = task.chunk.bundle.bundle_id
            bundle_map.setdefault(bundle_id, []).append(task)

        for tasks in bundle_map.values():
            tasks.sort(key=lambda item: item.chunk.offset)
        return bundle_map

    @staticmethod
    def merge_ranges(tasks: list[GlobalChunkTask], gap_tolerance: int) -> list[ChunkRange]:
        """将相邻 chunk 任务按 gap 容忍度合并为 Range 请求."""
        valid_tasks = [task for task in tasks if task.chunk.size > 0]
        if not valid_tasks:
            return []

        ranges: list[ChunkRange] = []
        start = valid_tasks[0].chunk.offset
        end = start + valid_tasks[0].chunk.size - 1
        current_tasks: list[GlobalChunkTask] = [valid_tasks[0]]

        for task in valid_tasks[1:]:
            task_start = task.chunk.offset
            task_end = task_start + task.chunk.size - 1
            gap = task_start - (end + 1)

            # 相邻或小间隔 chunk 合并为同一请求，减少 HTTP 请求数量。
            if gap <= gap_tolerance:
                end = task_end
                current_tasks.append(task)
            else:
                ranges.append(ChunkRange(start=start, end=end, tasks=current_tasks))
                start = task_start
                end = task_end
                current_tasks = [task]

        ranges.append(ChunkRange(start=start, end=end, tasks=current_tasks))
        return ranges

    def build_bundle_jobs(self, files: list[PatcherFile]) -> list[BundleJob]:
        """把文件列表转换为 bundle 维度的下载作业列表."""
        bundle_map = self.build_global_task_map(files)
        jobs: list[BundleJob] = []

        for bundle_id, tasks in bundle_map.items():
            ranges = self.merge_ranges(tasks, self.manifest.gap_tolerance)
            if not ranges:
                continue
            for i in range(0, len(ranges), self.manifest.max_ranges_per_request):
                jobs.append(BundleJob(bundle_id=bundle_id, ranges=ranges[i : i + self.manifest.max_ranges_per_request]))

        return jobs

    @staticmethod
    def job_total_bytes(job: BundleJob) -> int:
        """计算单个 bundle 作业覆盖的总字节数（压缩数据）。."""
        return sum(chunk_range.end - chunk_range.start + 1 for chunk_range in job.ranges)

    @staticmethod
    async def emit_progress(
        progress_callback: ProgressCallback | None,
        progress: DownloadProgress,
    ) -> None:
        """触发进度回调，兼容同步与异步回调函数.

        Args:
            progress_callback: 进度回调；可为同步或异步函数。
            progress: 当前进度快照。
        """
        if progress_callback is None:
            return

        result = progress_callback(progress)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def build_range_header(ranges: list[ChunkRange]) -> str:
        """构建 HTTP Range 请求头值."""
        return "bytes=" + ",".join(f"{chunk_range.start}-{chunk_range.end}" for chunk_range in ranges)

    @staticmethod
    def dynamic_request_timeout(
        *,
        total_bytes: int,
        base_timeout_seconds: int,
        min_transfer_speed_bytes: int,
        max_timeout_seconds: int,
    ) -> aiohttp.ClientTimeout:
        """按请求体积估算超时，避免大包固定超时误判."""
        size_factor = total_bytes / float(min_transfer_speed_bytes)
        timeout_seconds = base_timeout_seconds + int(size_factor)
        timeout_seconds = min(timeout_seconds, max_timeout_seconds)
        timeout_seconds = max(timeout_seconds, base_timeout_seconds)
        return aiohttp.ClientTimeout(total=timeout_seconds, sock_connect=30, sock_read=None)

    @staticmethod
    def extract_ranges_from_full_body(payload: bytes, ranges: list[ChunkRange], bundle_id: int) -> list[bytes]:
        """从完整响应体中切分出每个 Range 对应的子段数据."""
        outputs: list[bytes] = []
        payload_len = len(payload)
        for chunk_range in ranges:
            if payload_len < chunk_range.end + 1:
                raise DownloadError(
                    f"完整内容不足以切片range: bundle_id={bundle_id}, range={chunk_range.start}-{chunk_range.end}, "
                    f"payload_len={payload_len}"
                )
            outputs.append(payload[chunk_range.start : chunk_range.end + 1])
        return outputs

    async def parse_multipart_response(
        self,
        response: aiohttp.ClientResponse,
        ranges: list[ChunkRange],
        bundle_id: int,
    ) -> list[bytes]:
        """解析 multipart/byteranges 响应并按请求顺序返回数据块."""
        reader = aiohttp.MultipartReader.from_response(response)
        index_by_range = {(chunk_range.start, chunk_range.end): idx for idx, chunk_range in enumerate(ranges)}
        mapped_parts: list[bytes | None] = [None] * len(ranges)
        fallback_parts: list[bytes] = []

        while True:
            part = await reader.next()
            if part is None:
                break

            payload = await part.read(decode=False)
            content_range = part.headers.get(aiohttp.hdrs.CONTENT_RANGE, "").strip()

            mapped = False
            if content_range:
                match = self.CONTENT_RANGE_REGEX.match(content_range)
                if match:
                    start = int(match.group(1))
                    end = int(match.group(2))
                    idx = index_by_range.get((start, end))
                    if idx is not None and mapped_parts[idx] is None:
                        mapped_parts[idx] = payload
                        mapped = True

            if not mapped:
                fallback_parts.append(payload)

        # 兼容部分 CDN 返回缺失或无序 Content-Range 头的情况，按剩余顺序兜底映射。
        for idx, value in enumerate(mapped_parts):
            if value is None:
                if not fallback_parts:
                    raise DownloadError(f"multipart段数不足: bundle_id={bundle_id}, expected={len(ranges)}")
                mapped_parts[idx] = fallback_parts.pop(0)

        if fallback_parts:
            raise DownloadError(
                f"multipart段数过多: bundle_id={bundle_id}, expected={len(ranges)}, actual>{len(ranges)}"
            )

        return [part for part in mapped_parts if part is not None]

    async def fetch_ranges_data(
        self,
        session: aiohttp.ClientSession,
        bundle_id: int,
        ranges: list[ChunkRange],
    ) -> list[bytes]:
        """请求并返回一个 bundle 中多个 Range 的压缩数据."""
        if not ranges:
            return []

        url = urljoin(self.manifest.bundle_url, f"{bundle_id:016X}.bundle")
        range_header = self.build_range_header(ranges)
        total_bytes = sum(chunk_range.end - chunk_range.start + 1 for chunk_range in ranges)
        request_timeout = self.dynamic_request_timeout(
            total_bytes=total_bytes,
            base_timeout_seconds=self.manifest.DEFAULT_BASE_TIMEOUT_SECONDS,
            min_transfer_speed_bytes=self.manifest.DEFAULT_MIN_TRANSFER_SPEED_BYTES,
            max_timeout_seconds=self.manifest.DEFAULT_MAX_TIMEOUT_SECONDS,
        )

        try:
            headers = {
                "Range": range_header,
                "Accept-Encoding": "identity",
            }

            async with session.get(url, headers=headers, timeout=request_timeout) as response:
                if response.status not in (200, 206):
                    raise DownloadError(f"HTTP状态异常: {response.status}, bundle_id={bundle_id}")

                if response.status == 200:
                    payload = await response.read()
                    range_payloads = self.extract_ranges_from_full_body(
                        payload,
                        ranges,
                        bundle_id,
                    )
                else:
                    content_type = response.headers.get(aiohttp.hdrs.CONTENT_TYPE, "").lower()
                    if content_type.startswith("multipart/"):
                        range_payloads = await self.parse_multipart_response(
                            response,
                            ranges,
                            bundle_id,
                        )
                    else:
                        payload = await response.read()
                        if len(ranges) != 1:
                            raise DownloadError(
                                f"多段range未返回multipart: bundle_id={bundle_id}, ranges={len(ranges)}"
                            )
                        range_payloads = [payload]

                if len(range_payloads) != len(ranges):
                    raise DownloadError(
                        f"range响应数量不匹配: bundle_id={bundle_id}, expected={len(ranges)}, "
                        f"actual={len(range_payloads)}"
                    )

                for chunk_range, payload in zip(ranges, range_payloads, strict=False):
                    expected_size = chunk_range.end - chunk_range.start + 1
                    if len(payload) != expected_size:
                        raise DownloadError(
                            f"下载range失败: bundle_id={bundle_id}, range={chunk_range.start}-{chunk_range.end}, "
                            f"actual={len(payload)}, expected={expected_size}"
                        )

                return range_payloads
        except (TimeoutError, aiohttp.ClientError, DownloadError) as exc:
            raise DownloadError(f"下载 bundle {bundle_id:016X} ranges 失败: {exc}") from exc

    async def process_bundle_job(
        self,
        session: aiohttp.ClientSession,
        job: BundleJob,
        file_pool: FileHandlePool,
    ) -> None:
        """执行单个 bundle 作业：下载、解压、校验并扇出写盘."""
        range_payloads = await self.fetch_ranges_data(
            session=session,
            bundle_id=job.bundle_id,
            ranges=job.ranges,
        )

        for chunk_range, range_data in zip(job.ranges, range_payloads, strict=False):
            for task in chunk_range.tasks:
                chunk = task.chunk
                offset_in_range = chunk.offset - chunk_range.start
                end = offset_in_range + chunk.size

                if end > len(range_data):
                    raise DownloadError(
                        f"range数据截断: bundle_id={job.bundle_id}, chunk_id={chunk.chunk_id}, "
                        f"offset={offset_in_range}, size={chunk.size}, data_len={len(range_data)}"
                    )

                compressed = range_data[offset_in_range:end]
                try:
                    data = await asyncio.to_thread(pyzstd.decompress, compressed)
                except pyzstd.ZstdError as exc:
                    raise DecompressError(
                        f"解压chunk失败: chunk_id={chunk.chunk_id}, bundle_id={chunk.bundle.bundle_id}"
                    ) from exc

                # 同一 chunk 可能扇出到多个文件，哈希只需按 (chunk_id, hash_type) 校验一次。
                verified_hash_keys = set()
                for verify_target in task.targets:
                    verify_key = (verify_target.chunk_id, verify_target.hash_type)
                    if verify_key in verified_hash_keys:
                        continue
                    self.manifest.validate_chunk_hash(
                        chunk_data=data,
                        chunk_id=verify_target.chunk_id,
                        hash_type=verify_target.hash_type,
                    )
                    verified_hash_keys.add(verify_key)

                for target in task.targets:
                    if len(data) != target.expected_len:
                        raise DecompressError(
                            f"解压大小不匹配: chunk_id={chunk.chunk_id}, expected={target.expected_len}, actual={len(data)}"
                        )

                    output = self.manifest.file_output(target.file)
                    await asyncio.to_thread(file_pool.write_at, output, data, target.file_offset)

    async def run_bundle_job_with_retry(
        self,
        session: aiohttp.ClientSession,
        job: BundleJob,
        file_pool: FileHandlePool,
    ) -> None:
        """执行 bundle 作业并按配置重试失败任务."""
        last_error: Exception | None = None
        for attempt in range(self.manifest.max_retries):
            try:
                await self.process_bundle_job(
                    session=session,
                    job=job,
                    file_pool=file_pool,
                )
                return
            except (DownloadError, DecompressError, OSError) as exc:
                last_error = exc
                if attempt == self.manifest.max_retries - 1:
                    break
                await asyncio.sleep(attempt + 1)

        raise DownloadError(
            f"bundle任务失败: bundle_id={job.bundle_id}, retries={self.manifest.max_retries}, error={last_error}"
        )

    def _build_results(
        self,
        target_files: list[PatcherFile],
        failed_bundle_ids: set[int] | None = None,
    ) -> tuple[bool, ...]:
        """根据本地文件状态与失败 bundle 列表构建最终结果."""
        failed_bundle_ids = failed_bundle_ids or set()
        results: list[bool] = []

        for target_file in target_files:
            if target_file.link:
                results.append(True)
                continue

            output = self.manifest.file_output(target_file)
            if not self.manifest.is_complete_file(target_file, output):
                results.append(False)
                continue

            has_failed_chunk = any(chunk.bundle.bundle_id in failed_bundle_ids for chunk in target_file.chunks)
            results.append(not has_failed_chunk)

        return tuple(results)

    async def download_files_concurrently(
        self,
        files: list[PatcherFile],
        concurrency_limit: int | None = None,
        raise_on_error: bool = True,
        progress_callback: ProgressCallback | None = None,
        progress_interval_seconds: float | None = 1.0,
    ) -> tuple[bool, ...]:
        """并发下载多个文件并返回逐文件结果.

        关键策略：
        1. 先按 chunk 去重，再按 bundle 聚合作业；
        2. 对同一 bundle 合并 range，减少请求次数；
        3. 下载后扇出到多个目标文件，避免重复解压与重复下载。

        Args:
            files: 目标文件列表。
            concurrency_limit: 并发 worker 数；不传时使用 manifest 默认值。
            raise_on_error: 是否在任意 bundle 失败时抛出批量异常。
            progress_callback: 可选下载进度回调，每个作业完成后触发一次。
            progress_interval_seconds: 时间周期上报间隔（秒）；<=0 或 None 表示禁用周期上报。

        Returns:
            与入参文件顺序一致的下载结果元组。

        Raises:
            DownloadBatchError: 当 `raise_on_error=True` 且存在作业失败时抛出。
        """
        if not files:
            return tuple()

        # 保持输入顺序去重，避免同一文件重复统计。
        seen_files: dict[str, PatcherFile] = {}
        ordered_files: list[PatcherFile] = []
        for file in files:
            if file.name not in seen_files:
                seen_files[file.name] = file
                ordered_files.append(file)

        pending_files: list[PatcherFile] = []
        for file in ordered_files:
            if file.link:
                continue
            output = self.manifest.file_output(file)
            if not self.manifest.is_complete_file(file, output):
                self.manifest.preallocate_file(file)
                pending_files.append(file)

        if not pending_files:
            return self._build_results(files)

        jobs = self.build_bundle_jobs(pending_files)
        if not jobs:
            return self._build_results(files)

        total_jobs = len(jobs)
        total_bytes = sum(self.job_total_bytes(job) for job in jobs)
        start_time = time.perf_counter()
        succeeded_jobs = 0
        failed_jobs = 0
        finished_jobs = 0
        finished_bytes = 0
        progress_lock = asyncio.Lock()

        def make_progress(phase: str, bundle_id: int | None = None) -> DownloadProgress:
            """构建当前时刻的下载进度快照."""
            elapsed_seconds = max(time.perf_counter() - start_time, 0.0)
            progress_ratio = finished_jobs / total_jobs if total_jobs > 0 else 1.0
            average_speed = finished_bytes / elapsed_seconds if elapsed_seconds > 0 else 0.0
            return DownloadProgress(
                phase=phase,
                total_jobs=total_jobs,
                finished_jobs=finished_jobs,
                succeeded_jobs=succeeded_jobs,
                failed_jobs=failed_jobs,
                total_bytes=total_bytes,
                finished_bytes=finished_bytes,
                progress=progress_ratio,
                elapsed_seconds=elapsed_seconds,
                average_speed_bytes_per_sec=average_speed,
                bundle_id=bundle_id,
            )

        effective_concurrency = (
            concurrency_limit if concurrency_limit is not None else self.manifest.concurrency_limit
        )
        worker_count = max(1, min(effective_concurrency, len(jobs)))
        connector = aiohttp.TCPConnector(
            limit=max(worker_count * 4, 16),
            limit_per_host=max(worker_count * 4, 16),
        )
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=None)
        file_pool = FileHandlePool(max_handles=max(worker_count * 8, 256))

        queue: asyncio.Queue[BundleJob] = asyncio.Queue()
        for job in jobs:
            queue.put_nowait(job)

        errors: list[BundleJobFailure] = []
        error_lock = asyncio.Lock()
        reporter_stop = asyncio.Event()
        reporter_task: asyncio.Task[None] | None = None

        interval_enabled = progress_interval_seconds is not None and progress_interval_seconds > 0
        interval_seconds = progress_interval_seconds if interval_enabled else 0.0

        async def worker() -> None:
            nonlocal failed_jobs, finished_bytes, finished_jobs, succeeded_jobs
            while True:
                try:
                    job = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                try:
                    await self.run_bundle_job_with_retry(
                        session=session,
                        job=job,
                        file_pool=file_pool,
                    )
                    job_bytes = self.job_total_bytes(job)
                    async with progress_lock:
                        succeeded_jobs += 1
                        finished_jobs += 1
                        finished_bytes += job_bytes
                        progress = make_progress("bundle_completed", bundle_id=job.bundle_id)
                    await self.emit_progress(progress_callback, progress)
                except Exception as exc:  # noqa: BLE001
                    async with error_lock:
                        errors.append(BundleJobFailure(bundle_id=job.bundle_id, error=exc))
                    async with progress_lock:
                        failed_jobs += 1
                        finished_jobs += 1
                        progress = make_progress("bundle_failed", bundle_id=job.bundle_id)
                    await self.emit_progress(progress_callback, progress)
                finally:
                    queue.task_done()

        async def periodic_progress_reporter() -> None:
            """按固定时间间隔上报进度，避免长尾任务无反馈."""
            if not interval_enabled:
                return

            while not reporter_stop.is_set():
                await asyncio.sleep(interval_seconds)
                if reporter_stop.is_set():
                    break
                async with progress_lock:
                    progress = make_progress("tick")
                await self.emit_progress(progress_callback, progress)

        try:
            await self.emit_progress(progress_callback, make_progress("start"))
            if progress_callback is not None and interval_enabled:
                reporter_task = asyncio.create_task(periodic_progress_reporter())
            async with aiohttp.ClientSession(connector=connector, timeout=timeout, auto_decompress=False) as session:
                workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
                await queue.join()
                await asyncio.gather(*workers)
        finally:
            reporter_stop.set()
            if reporter_task is not None:
                await reporter_task
            await asyncio.to_thread(file_pool.close)

        if errors:
            await self.emit_progress(progress_callback, make_progress("failed"))
            if raise_on_error:
                raise DownloadBatchError(errors)
            for failure in errors:
                logger.error(f"bundle下载失败: {failure.bundle_id:016X}, error={failure.error}")
            return self._build_results(files, failed_bundle_ids={failure.bundle_id for failure in errors})

        await self.emit_progress(progress_callback, make_progress("completed"))
        return self._build_results(files)


if TYPE_CHECKING:
    # 仅用于类型检查提示，避免运行时循环导入。
    from riotmanifest.manifest import PatcherChunk, PatcherFile, PatcherManifest
