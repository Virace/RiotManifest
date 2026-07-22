"""Manifest 下载调度实现.

该模块聚焦并发下载、Range 请求拼接、解压与写盘流程，
避免 `PatcherManifest` 同时承担“解析 + 下载调度”两类复杂职责。
"""

from __future__ import annotations

import asyncio
import inspect
import math
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urljoin

import aiohttp
import pyzstd
from loguru import logger

from riotmanifest.core.errors import BundleJobFailure, DecompressError, DownloadBatchError, DownloadError
from riotmanifest.downloader.file_pool import FileHandlePool
from riotmanifest.downloader.staging import commit_staging, discard_staging, staging_path


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


@dataclass(slots=True)
class ChunkDownloadResult:
    """按 chunk 条目下载的结构化结果.

    Attributes:
        failed_paths: 关联失败作业的文件相对路径集合；空集合表示全部成功。
        failures: bundle 维度的失败详情。`error` 为重试耗尽后的包装异常，
            底层原因（超时 / 连接重置 / HTTP 状态码等）沿 `__cause__` 链保留。
    """

    failed_paths: set[str]
    failures: list[BundleJobFailure]


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


@dataclass(slots=True)
class ChunkEntry:
    """(文件, chunk, 文件内解压域偏移) 三元组，验证与调度共享的最小单元."""

    file: PatcherFile
    chunk: PatcherChunk
    file_offset: int


def iter_chunk_entries(file: PatcherFile) -> Iterator[ChunkEntry]:
    """按 chunks 顺序累加 target_size，产出每个 chunk 的文件内写入偏移."""
    file_offset = 0
    for chunk in file.chunks:
        yield ChunkEntry(file=file, chunk=chunk, file_offset=file_offset)
        file_offset += chunk.target_size


@dataclass
class ChunkRange:
    """Bundle 内单段 Range 请求定义."""

    start: int
    end: int
    tasks: list[GlobalChunkTask] = field(default_factory=list)


@dataclass
class BundleJob:
    """Bundle 下载任务.

    Attributes:
        full_bundle: True 表示整包下载：请求不带 Range 头拉取完整 bundle，
            本地按 `ranges` 切片；此时 `total_bytes` 为 bundle 总大小。
    """

    bundle_id: int
    ranges: list[ChunkRange] = field(default_factory=list)
    total_bytes: int = 0
    full_bundle: bool = False


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

    def build_global_task_map(
        self,
        files: list[PatcherFile],
        *,
        entries: Iterable[ChunkEntry] | None = None,
    ) -> dict[int, list[GlobalChunkTask]]:
        """按 ChunkID 去重并构建全局任务映射.

        Args:
            files: 需要下载的目标文件列表。
            entries: 预构建的 chunk 条目；给定时仅对条目建任务（`files` 被忽略），
                用于增量场景只下载 miss 的 chunk。

        Returns:
            以 bundle_id 分组的任务映射。
        """
        if entries is None:
            entries = (entry for file in files for entry in iter_chunk_entries(file))

        chunk_index: dict[int, GlobalChunkTask] = {}
        for entry in entries:
            chunk = entry.chunk
            target = WriteTarget(
                file=entry.file,
                file_offset=entry.file_offset,
                expected_len=chunk.target_size,
                chunk_id=chunk.chunk_id,
                hash_type=entry.file.chunk_hash_types.get(chunk.chunk_id, 0),
            )
            if chunk.chunk_id in chunk_index:
                chunk_index[chunk.chunk_id].targets.append(target)
            else:
                chunk_index[chunk.chunk_id] = GlobalChunkTask(chunk=chunk, targets=[target])

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

    def build_bundle_jobs(
        self,
        files: list[PatcherFile],
        *,
        entries: Iterable[ChunkEntry] | None = None,
    ) -> list[BundleJob]:
        """把文件列表（或预构建 chunk 条目）转换为 bundle 维度的下载作业列表.

        需下载字节占 bundle 总大小比例达到 `full_bundle_threshold` 时，
        该 bundle 收敛为单个整包作业（不带 Range 的完整 GET）；稀疏覆盖
        的 bundle 维持 Range 作业——整包在稀疏场景只增加字节不减请求数。
        """
        bundle_map = self.build_global_task_map(files, entries=entries)
        threshold = getattr(self.manifest, "full_bundle_threshold", None)
        jobs: list[BundleJob] = []

        for bundle_id, tasks in bundle_map.items():
            ranges = self.merge_ranges(tasks, self.manifest.gap_tolerance)
            if not ranges:
                continue

            if threshold is not None:
                bundle_chunks = tasks[0].chunk.bundle.chunks
                bundle_size = bundle_chunks[-1].offset + bundle_chunks[-1].size if bundle_chunks else 0
                covered = sum(chunk_range.end - chunk_range.start + 1 for chunk_range in ranges)
                if bundle_size > 0 and covered / bundle_size >= threshold:
                    jobs.append(
                        BundleJob(
                            bundle_id=bundle_id,
                            ranges=ranges,
                            total_bytes=bundle_size,
                            full_bundle=True,
                        )
                    )
                    continue

            for i in range(0, len(ranges), self.manifest.max_ranges_per_request):
                job_ranges = ranges[i : i + self.manifest.max_ranges_per_request]
                total_bytes = sum(chunk_range.end - chunk_range.start + 1 for chunk_range in job_ranges)
                jobs.append(
                    BundleJob(
                        bundle_id=bundle_id,
                        ranges=job_ranges,
                        total_bytes=total_bytes,
                    )
                )

        # 先执行大作业可显著降低 worker 队列尾部“少量超大包”导致的长尾。
        jobs.sort(key=lambda job: (-job.total_bytes, job.bundle_id))

        return jobs

    @staticmethod
    def job_total_bytes(job: BundleJob) -> int:
        """计算单个 bundle 作业覆盖的总字节数（压缩数据）。."""
        if job.total_bytes > 0:
            return job.total_bytes
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
        sock_read_timeout_seconds: int,
    ) -> aiohttp.ClientTimeout:
        """按请求体积估算超时，避免大包固定超时误判."""
        safe_min_transfer_speed = max(1, min_transfer_speed_bytes)
        size_factor = total_bytes / float(safe_min_transfer_speed)
        timeout_seconds = base_timeout_seconds + math.ceil(size_factor)
        timeout_seconds = min(timeout_seconds, max_timeout_seconds)
        timeout_seconds = max(timeout_seconds, base_timeout_seconds)
        # 限制单次读阻塞时间，避免个别连接长时间“半死不活”拖慢全局收尾。
        sock_read_timeout = min(max(1, sock_read_timeout_seconds), timeout_seconds)
        return aiohttp.ClientTimeout(
            total=timeout_seconds,
            sock_connect=30,
            sock_read=sock_read_timeout,
        )

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
        unmapped = sum(1 for value in mapped_parts if value is None)
        if unmapped:
            logger.debug(
                "multipart缺失Content-Range，按顺序兜底映射: bundle_id={:016X}, parts={}/{}",
                bundle_id,
                unmapped,
                len(ranges),
            )
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
        *,
        full_bundle: bool = False,
        expected_bytes: int | None = None,
        attempt: int = 0,
    ) -> list[bytes]:
        """请求并返回一个 bundle 中多个 Range 的压缩数据.

        Args:
            session: 复用的 HTTP 会话。
            bundle_id: 目标 bundle。
            ranges: 请求段定义；整包模式下仅用于本地切片。
            full_bundle: True 时不携带 Range 头拉取完整 bundle 并本地切片。
            expected_bytes: 超时估算用的预期传输量；整包模式传 bundle 总大小。
            attempt: 当前重试序号。配置多个等价 bundle URL 时按
                `(bundle_id + attempt) % len(urls)` 选择基础 URL——作业确定性
                分摊到各镜像域名，重试自动切换下一个（跨供应商 failover）。
        """
        if not ranges:
            return []

        base_urls = getattr(self.manifest, "bundle_urls", None) or [self.manifest.bundle_url]
        base_url = base_urls[(bundle_id + attempt) % len(base_urls)]
        url = urljoin(base_url, f"{bundle_id:016X}.bundle")
        total_bytes = (
            expected_bytes
            if expected_bytes is not None
            else sum(chunk_range.end - chunk_range.start + 1 for chunk_range in ranges)
        )
        request_timeout = self.dynamic_request_timeout(
            total_bytes=total_bytes,
            base_timeout_seconds=self.manifest.DEFAULT_BASE_TIMEOUT_SECONDS,
            min_transfer_speed_bytes=self.manifest.DEFAULT_MIN_TRANSFER_SPEED_BYTES,
            max_timeout_seconds=self.manifest.DEFAULT_MAX_TIMEOUT_SECONDS,
            sock_read_timeout_seconds=self.manifest.DEFAULT_SOCK_READ_TIMEOUT_SECONDS,
        )

        try:
            headers = {"Accept-Encoding": "identity"}
            if not full_bundle:
                headers["Range"] = self.build_range_header(ranges)

            async with session.get(url, headers=headers, timeout=request_timeout) as response:
                if response.status not in (200, 206):
                    raise DownloadError(f"HTTP状态异常: {response.status}, bundle_id={bundle_id}")

                if full_bundle or response.status == 200:
                    if not full_bundle:
                        logger.debug(
                            "range请求降级为200完整体，本地切片: bundle_id={:016X}, ranges={}",
                            bundle_id,
                            len(ranges),
                        )
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
        *,
        attempt: int = 0,
    ) -> None:
        """执行单个 bundle 作业：下载、解压、校验并扇出写盘."""
        range_payloads = await self.fetch_ranges_data(
            session=session,
            bundle_id=job.bundle_id,
            ranges=job.ranges,
            full_bundle=job.full_bundle,
            expected_bytes=job.total_bytes if job.full_bundle else None,
            attempt=attempt,
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

                    output = staging_path(self.manifest.file_output(target.file))
                    await asyncio.to_thread(file_pool.write_at, output, data, target.file_offset)

    async def run_bundle_job_with_retry(
        self,
        session: aiohttp.ClientSession,
        job: BundleJob,
        file_pool: FileHandlePool,
    ) -> int:
        """执行 bundle 作业并按配置重试失败任务.

        Returns:
            成功前经历的重试次数；0 表示首次尝试即成功。
        """
        last_error: Exception | None = None
        for attempt in range(self.manifest.max_retries):
            try:
                await self.process_bundle_job(
                    session=session,
                    job=job,
                    file_pool=file_pool,
                    attempt=attempt,
                )
                return attempt
            except (DownloadError, DecompressError, OSError) as exc:
                last_error = exc
                if attempt == self.manifest.max_retries - 1:
                    break
                delay = attempt + 1
                logger.warning(
                    "bundle作业重试: {:016X}, attempt={}/{}, delay={}s, error={}",
                    job.bundle_id,
                    attempt + 1,
                    self.manifest.max_retries,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)

        raise DownloadError(
            f"bundle任务失败: bundle_id={job.bundle_id}, retries={self.manifest.max_retries}, error={last_error}"
        ) from last_error

    def _finalize_staging(
        self,
        pending_files: list[PatcherFile],
        failed_paths: set[str],
    ) -> None:
        """批次收尾：成功文件提交 staging，失败文件丢弃 staging 并保留旧文件."""
        for file in pending_files:
            output = self.manifest.file_output(file)
            if file.name in failed_paths:
                discard_staging(output)
            else:
                commit_staging(output)

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

        # 语义为“给什么下什么”：是否跳过由上层（如 update 编排器）决定。
        pending_files = [file for file in ordered_files if not file.link]
        for file in pending_files:
            self.manifest.preallocate_file(file)

        if not pending_files:
            return self._build_results(files)

        jobs = self.build_bundle_jobs(pending_files)
        if not jobs:
            self._finalize_staging(pending_files, set())
            return self._build_results(files)

        errors = await self._run_jobs(
            jobs,
            concurrency_limit=concurrency_limit,
            progress_callback=progress_callback,
            progress_interval_seconds=progress_interval_seconds,
        )

        # 无论批次成败都先收尾 staging：成功文件提交、失败文件丢弃并保留旧文件。
        failed_bundle_ids = {failure.bundle_id for failure in errors}
        failed_paths = {
            file.name
            for file in pending_files
            if any(chunk.bundle.bundle_id in failed_bundle_ids for chunk in file.chunks)
        }
        await asyncio.to_thread(self._finalize_staging, pending_files, failed_paths)

        if errors:
            if raise_on_error:
                raise DownloadBatchError(errors)
            return self._build_results(files, failed_bundle_ids=failed_bundle_ids)

        return self._build_results(files)

    async def download_chunk_entries(
        self,
        entries: list[ChunkEntry],
        *,
        concurrency_limit: int | None = None,
        raise_on_error: bool = True,
        progress_callback: ProgressCallback | None = None,
        progress_interval_seconds: float | None = 1.0,
        manage_staging: bool = False,
    ) -> ChunkDownloadResult:
        """按预构建 chunk 条目下载并写入各文件的 staging.

        Args:
            entries: 待下载的 (文件, chunk, 偏移) 条目，通常来自本地验证的 miss 列表。
            concurrency_limit: 并发 worker 数；不传时使用 manifest 默认值。
            raise_on_error: 是否在任意 bundle 失败时抛出批量异常。
            progress_callback: 可选下载进度回调。
            progress_interval_seconds: 时间周期上报间隔（秒）。
            manage_staging: True 时由本方法负责 staging 的预分配与提交/丢弃；
                False（默认）时 staging 生命周期完全归调用方（如 update 编排器）。

        Returns:
            失败文件路径集合与 bundle 维度失败详情；`raise_on_error=False` 时
            下游依赖 `failures` 获取每个失败的原始异常。

        Raises:
            DownloadBatchError: 当 `raise_on_error=True` 且存在作业失败时抛出。
        """
        entries = list(entries)
        if not entries:
            return ChunkDownloadResult(failed_paths=set(), failures=[])

        involved_files: dict[str, PatcherFile] = {}
        for entry in entries:
            involved_files.setdefault(entry.file.name, entry.file)

        if manage_staging:
            for file in involved_files.values():
                self.manifest.preallocate_file(file)

        jobs = self.build_bundle_jobs([], entries=entries)
        errors = await self._run_jobs(
            jobs,
            concurrency_limit=concurrency_limit,
            progress_callback=progress_callback,
            progress_interval_seconds=progress_interval_seconds,
        )

        failed_bundle_ids = {failure.bundle_id for failure in errors}
        failed_paths = {entry.file.name for entry in entries if entry.chunk.bundle.bundle_id in failed_bundle_ids}

        if manage_staging:
            await asyncio.to_thread(self._finalize_staging, list(involved_files.values()), failed_paths)

        if errors and raise_on_error:
            raise DownloadBatchError(errors)
        return ChunkDownloadResult(failed_paths=failed_paths, failures=errors)

    async def _run_jobs(
        self,
        jobs: list[BundleJob],
        *,
        concurrency_limit: int | None,
        progress_callback: ProgressCallback | None,
        progress_interval_seconds: float | None,
    ) -> list[BundleJobFailure]:
        """执行本清单的 bundle 作业集（多组运行器的单组特例）."""
        effective = concurrency_limit if concurrency_limit is not None else self.manifest.concurrency_limit
        grouped = await run_job_groups(
            [JobGroup(scheduler=self, jobs=jobs)],
            concurrency_limit=effective,
            progress_callback=progress_callback,
            progress_interval_seconds=progress_interval_seconds,
        )
        return grouped[0]


@dataclass(slots=True)
class JobGroup:
    """归属同一调度器（同一清单）的一组 bundle 作业."""

    scheduler: DownloadScheduler
    jobs: list[BundleJob]


async def run_job_groups(
    groups: list[JobGroup],
    *,
    concurrency_limit: int | None = None,
    progress_callback: ProgressCallback | None = None,
    progress_interval_seconds: float | None = 1.0,
) -> list[list[BundleJobFailure]]:
    """跨清单合并执行多组 bundle 作业：单 worker 池、单进度流.

    进度事件的 total/finished 为所有组合计（字节为压缩域）；
    `concurrency_limit` 缺省时取各组清单 `concurrency_limit` 的最大值。

    Returns:
        与 groups 一一对应的失败列表。
    """
    if not groups:
        return []

    items: list[tuple[int, BundleJob]] = [
        (group_index, job) for group_index, group in enumerate(groups) for job in group.jobs
    ]
    # 先执行大作业可显著降低 worker 队列尾部“少量超大包”导致的长尾。
    items.sort(key=lambda item: (-DownloadScheduler.job_total_bytes(item[1]), item[1].bundle_id))

    total_jobs = len(items)
    total_bytes = sum(DownloadScheduler.job_total_bytes(job) for _, job in items)
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
        concurrency_limit
        if concurrency_limit is not None
        else max(group.scheduler.manifest.concurrency_limit for group in groups)
    )
    worker_count = max(1, min(effective_concurrency, total_jobs))
    logger.info("下载批次开始: jobs={}, total_bytes={}, workers={}", total_jobs, total_bytes, worker_count)
    # 任一清单注入了自定义 resolver 即对整个批次生效；此时关闭 aiohttp 内建
    # DNS 缓存，保证 resolver 每次连接都能轮转返回不同的边缘 IP。
    resolver = next(
        (
            group.scheduler.manifest.resolver
            for group in groups
            if getattr(group.scheduler.manifest, "resolver", None) is not None
        ),
        None,
    )
    connector = aiohttp.TCPConnector(
        limit=max(worker_count * 4, 16),
        limit_per_host=max(worker_count * 4, 16),
        resolver=resolver,
        use_dns_cache=resolver is None,
    )
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=None)
    file_pool = FileHandlePool(max_handles=max(worker_count * 8, 256))

    queue: asyncio.Queue[tuple[int, BundleJob]] = asyncio.Queue()
    for item in items:
        queue.put_nowait(item)

    failures: list[list[BundleJobFailure]] = [[] for _ in groups]
    error_lock = asyncio.Lock()
    reporter_stop = asyncio.Event()
    reporter_task: asyncio.Task[None] | None = None

    interval_enabled = progress_interval_seconds is not None and progress_interval_seconds > 0
    interval_seconds = progress_interval_seconds if interval_enabled else 0.0

    async def worker() -> None:
        nonlocal failed_jobs, finished_bytes, finished_jobs, succeeded_jobs
        while True:
            try:
                group_index, job = queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            scheduler = groups[group_index].scheduler
            job_started = time.perf_counter()
            try:
                retries = await scheduler.run_bundle_job_with_retry(
                    session=session,
                    job=job,
                    file_pool=file_pool,
                )
                job_bytes = DownloadScheduler.job_total_bytes(job)
                job_elapsed = max(time.perf_counter() - job_started, 1e-9)
                logger.debug(
                    "bundle作业完成: {:016X}, bytes={}, elapsed={:.2f}s, speed={:.0f}B/s, retries={}",
                    job.bundle_id,
                    job_bytes,
                    job_elapsed,
                    job_bytes / job_elapsed,
                    retries,
                )
                async with progress_lock:
                    succeeded_jobs += 1
                    finished_jobs += 1
                    finished_bytes += job_bytes
                    progress = make_progress("bundle_completed", bundle_id=job.bundle_id)
                await DownloadScheduler.emit_progress(progress_callback, progress)
            except Exception as exc:  # noqa: BLE001
                logger.error("bundle下载失败: {:016X}, error={}", job.bundle_id, exc)
                async with error_lock:
                    failures[group_index].append(BundleJobFailure(bundle_id=job.bundle_id, error=exc))
                async with progress_lock:
                    failed_jobs += 1
                    finished_jobs += 1
                    progress = make_progress("bundle_failed", bundle_id=job.bundle_id)
                await DownloadScheduler.emit_progress(progress_callback, progress)
            finally:
                queue.task_done()

    async def periodic_progress_reporter() -> None:
        """按固定时间间隔上报进度，避免长尾任务无反馈."""
        if not interval_enabled:
            return

        # 等停止事件带超时而非裸 sleep：批次收尾不必等满一个间隔。
        while not reporter_stop.is_set():
            try:
                await asyncio.wait_for(reporter_stop.wait(), timeout=interval_seconds)
                break
            except asyncio.TimeoutError:  # noqa: UP041 - 3.10 下两者不同类
                pass
            async with progress_lock:
                progress = make_progress("tick")
            await DownloadScheduler.emit_progress(progress_callback, progress)

    try:
        await DownloadScheduler.emit_progress(progress_callback, make_progress("start"))
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

    logger.info(
        "下载批次结束: succeeded={}, failed={}, finished_bytes={}, elapsed={:.1f}s",
        succeeded_jobs,
        failed_jobs,
        finished_bytes,
        max(time.perf_counter() - start_time, 0.0),
    )
    if any(failures):
        await DownloadScheduler.emit_progress(progress_callback, make_progress("failed"))
    else:
        await DownloadScheduler.emit_progress(progress_callback, make_progress("completed"))
    return failures


if TYPE_CHECKING:
    # 仅用于类型检查提示，避免运行时循环导入。
    from riotmanifest.manifest import PatcherChunk, PatcherFile, PatcherManifest
