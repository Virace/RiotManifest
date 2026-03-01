# -*- coding: utf-8 -*-
# @Author  : Virace
# @Email   : Virace@aliyun.com
# @Site    : x-item.com
# @Software: Pycharm
# @Create  : 2024/3/12 22:46
# @Update  : 2025/3/11 16:17
# @Detail  : manifest.py

import asyncio
from collections import OrderedDict
import hashlib
import hmac
import io
import os
import os.path
import re
import struct
import threading
from dataclasses import dataclass, field
from typing import BinaryIO, Iterable, Optional, Tuple
from typing import Dict, List, Union
from urllib.parse import urljoin, urlparse

import aiohttp
import pyzstd
from loguru import logger
from riotmanifest.http_client import HttpClientError, http_get_bytes

try:
    import blake3
except ImportError:
    blake3 = None

RETRY_LIMIT = 5
HASH_TYPE_SHA512 = 1
HASH_TYPE_SHA256 = 2
HASH_TYPE_HKDF = 3
HASH_TYPE_BLAKE3 = 4

StrPath = Union[str, "os.PathLike[str]"]


class DownloadError(Exception):
    pass


class DecompressError(Exception):
    pass


@dataclass
class BundleJobFailure:
    bundle_id: int
    error: Exception


class DownloadBatchError(DownloadError):
    """批量下载存在失败任务时抛出的异常。"""

    def __init__(self, failures: List[BundleJobFailure]):
        """初始化批量下载异常。

        Args:
            failures: 失败的 bundle 任务列表。
        """
        self.failures = failures
        summary = ", ".join(f"{failure.bundle_id:016X}:{failure.error}" for failure in failures[:5])
        if len(failures) > 5:
            summary = f"{summary}, ... total={len(failures)}"
        super().__init__(f"存在 {len(failures)} 个 bundle 任务失败: {summary}")


class BinaryParser:
    """Helper class to read from binary file object"""

    def __init__(self, f: BinaryIO):
        self.f = f

    def tell(self):
        return self.f.tell()

    def seek(self, position: int):
        self.f.seek(position, 0)

    def skip(self, amount: int):
        self.f.seek(amount, 1)

    def rewind(self, amount: int):
        self.f.seek(-amount, 1)

    def unpack(self, fmt: str):
        length = struct.calcsize(fmt)
        return struct.unpack(fmt, self.f.read(length))

    def raw(self, length: int):
        return self.f.read(length)

    def unpack_string(self):
        """Unpack string prefixed by its 32-bit length"""
        return self.f.read(self.unpack("<L")[0]).decode("utf-8")


class PatcherChunk:
    def __init__(
        self,
        chunk_id: int,
        bundle: "PatcherBundle",
        offset: int,
        size: int,
        target_size: int,
    ):
        """

        :param chunk_id:
        :param bundle:
        :param offset:
        :param size:
        :param target_size:
        """
        self.chunk_id: int = chunk_id
        self.bundle: "PatcherBundle" = bundle
        self.offset: int = offset
        self.size: int = size
        self.target_size: int = target_size

    def __hash__(self):
        return self.chunk_id


class PatcherBundle:
    def __init__(self, bundle_id: int):
        """

        :param bundle_id:
        """
        self.bundle_id: int = bundle_id
        self.chunks: List[PatcherChunk] = []

    def add_chunk(self, chunk_id: int, size: int, target_size: int):
        try:
            last_chunk = self.chunks[-1]
            offset = last_chunk.offset + last_chunk.size
        except IndexError:
            offset = 0
        self.chunks.append(PatcherChunk(chunk_id, self, offset, size, target_size))


@dataclass
class WriteTarget:
    file: "PatcherFile"
    file_offset: int
    expected_len: int
    chunk_id: int
    hash_type: int


@dataclass
class GlobalChunkTask:
    chunk: PatcherChunk
    targets: List[WriteTarget] = field(default_factory=list)


@dataclass
class ChunkRange:
    start: int
    end: int
    tasks: List[GlobalChunkTask] = field(default_factory=list)


@dataclass
class BundleJob:
    bundle_id: int
    ranges: List[ChunkRange] = field(default_factory=list)


@dataclass
class _HandleEntry:
    file_obj: BinaryIO
    file_lock: threading.Lock
    refs: int = 0
    evicted: bool = False


class FileHandlePool:
    """轻量文件句柄池，避免每次写入都重复 open/close。"""

    def __init__(self, max_handles: int = 500):
        self.max_handles = max(1, max_handles)
        self._handles: "OrderedDict[str, _HandleEntry]" = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _close_entry(entry: _HandleEntry):
        with entry.file_lock:
            entry.file_obj.close()

    def _evict_one_locked(self) -> List[_HandleEntry]:
        if not self._handles:
            return []

        _, entry = self._handles.popitem(last=False)
        entry.evicted = True
        if entry.refs == 0:
            return [entry]
        return []

    def _acquire(self, path: StrPath) -> _HandleEntry:
        norm_path = os.fspath(path)
        to_close: List[_HandleEntry] = []
        entry: Optional[_HandleEntry] = None
        try:
            with self._lock:
                if norm_path in self._handles:
                    entry = self._handles.pop(norm_path)
                    entry.refs += 1
                    self._handles[norm_path] = entry
                    return entry

                while len(self._handles) >= self.max_handles:
                    to_close.extend(self._evict_one_locked())

                file_obj = open(norm_path, "r+b", buffering=0)
                entry = _HandleEntry(file_obj=file_obj, file_lock=threading.Lock(), refs=1)
                self._handles[norm_path] = entry
                return entry
        finally:
            for close_entry in to_close:
                self._close_entry(close_entry)

    def _release(self, entry: _HandleEntry):
        should_close = False
        with self._lock:
            entry.refs -= 1
            should_close = entry.refs == 0 and entry.evicted
        if should_close:
            self._close_entry(entry)

    def write_at(self, path: StrPath, data: bytes, offset: int):
        entry = self._acquire(path)
        try:
            with entry.file_lock:
                entry.file_obj.seek(offset)
                entry.file_obj.write(data)
        finally:
            self._release(entry)

    def close(self):
        to_close: List[_HandleEntry] = []
        with self._lock:
            handles = list(self._handles.values())
            self._handles.clear()
            for entry in handles:
                entry.evicted = True
                if entry.refs == 0:
                    to_close.append(entry)

        for entry in to_close:
            self._close_entry(entry)


class PatcherFile:
    def __init__(
        self,
        name: str,
        size: int,
        link: str,
        flags: Optional[List[str]],
        chunks: List[PatcherChunk],
        manifest: "PatcherManifest",
        chunk_hash_types: Optional[Dict[int, int]] = None,
    ):
        """初始化补丁文件对象。

        `hexdigest()` 不是文件字节哈希，而是由 chunk_id 列表计算出的摘要，
        可用于下载前判断文件内容是否一致。

        Args:
            name: 文件相对路径。
            size: 文件字节大小。
            link: 链接文件目标（为空表示普通文件）。
            flags: 文件标志列表。
            chunks: 文件对应的 chunk 列表。
            manifest: 所属 manifest 对象。
            chunk_hash_types: chunk_id 到 hash_type 的映射。
        """
        self.name: str = name
        self.size: int = size
        self.link: str = link
        self.flags: Optional[List[str]] = flags

        self.chunks: List[PatcherChunk] = chunks
        self.manifest: "PatcherManifest" = manifest
        self.chunk_hash_types: Dict[int, int] = chunk_hash_types or {}

        self.chunk_cache = {}

    def hexdigest(self):
        """Compute a hash unique for this file content"""
        m = hashlib.sha1()
        for chunk in self.chunks:
            m.update(b"%016X" % chunk.chunk_id)
        return m.hexdigest()

    @staticmethod
    def langs_predicate(langs):
        """Return a predicate function for a locale filtering parameter"""
        if langs is False:
            # assume only locales flags follow this pattern
            return lambda f: f.flags is None or not any("_" in f and len(f) == 5 for f in f.flags)
        elif langs is True:
            return lambda f: True
        else:
            lang = langs.lower()  # compare lowercased
            return lambda f: f.flags is not None and any(f.lower() == lang for f in f.flags)

    def _verify_file(self, path: StrPath) -> bool:
        """
        按文件大小进行快速校验。

        :param path: 文件路径
        :return: 校验通过返回 True，否则返回 False。
        """
        if os.path.isfile(path) and os.path.getsize(path) == self.size:
            logger.info(f"{self.name}，校验通过")
            return True
        return False

    async def download_file(self, path: StrPath, concurrency_limit: Optional[int] = None) -> bool:
        """下载单个文件（委托给 Manifest 全局调度器）。

        Args:
            path: 文件保存目录。
            concurrency_limit: 覆盖 manifest 默认并发数；为 None 时使用 manifest 配置。

        Returns:
            下载成功返回 True，否则返回 False。
        """
        self.manifest.path = path
        results = await self.manifest.download_files_concurrently(
            [self],
            concurrency_limit=concurrency_limit,
        )
        return bool(results and results[0])

    def download_chunk(self, chunk: "PatcherChunk") -> bytes:
        """下载并解压单个 chunk（同步方法）。

        Args:
            chunk: 需要下载的 chunk 对象。

        Returns:
            解压后的 chunk 字节数据。

        Raises:
            DownloadError: 下载重试耗尽后仍失败。
            DecompressError: 解压或哈希校验失败。
        """
        if chunk.chunk_id in self.chunk_cache:
            return self.chunk_cache[chunk.chunk_id]

        url = urljoin(self.manifest.bundle_url, f"{chunk.bundle.bundle_id:016X}.bundle")
        content = b""
        for attempt in range(RETRY_LIMIT):
            try:
                headers = {"Range": f"bytes={chunk.offset}-{chunk.offset + chunk.size - 1}"}
                content = http_get_bytes(url, headers=headers)

                if len(content) != chunk.size:
                    raise DownloadError(
                        f"下载的chunk {chunk.chunk_id}失败，获取到 {len(content)} 字节，期望 {chunk.size} 字节，"
                        f"bundle_id为 {chunk.bundle.bundle_id}"
                    )
                break
            except HttpClientError as e:
                if attempt == RETRY_LIMIT - 1:
                    raise DownloadError(
                        f"在 {RETRY_LIMIT} 次尝试后，下载chunk {chunk.chunk_id}失败，bundle_id为 {chunk.bundle.bundle_id}"
                    ) from e

        try:
            decompressed_data = pyzstd.decompress(content)
        except pyzstd.ZstdError as e:
            raise DecompressError(f"解压缩chunk {chunk.chunk_id}时出错，bundle_id为 {chunk.bundle.bundle_id}") from e

        hash_type = self.chunk_hash_types.get(chunk.chunk_id, 0)
        self.manifest._validate_chunk_hash(  # pylint: disable=protected-access
            chunk_data=decompressed_data,
            chunk_id=chunk.chunk_id,
            hash_type=hash_type,
        )

        self.chunk_cache[chunk.chunk_id] = decompressed_data
        return decompressed_data

    def download_chunks(self, chunks: List["PatcherChunk"]) -> bytes:
        """
        下载并解压缩多个chunk，并将它们的内容拼接成一个字节串。

        :param chunks: 需要下载的PatcherChunk对象列表。
        :return: 拼接后的解压缩内容字节数据。
        """
        combined_data = b""
        for chunk in chunks:
            combined_data += self.download_chunk(chunk)
        return combined_data


class PatcherManifest:
    DEFAULT_GAP_TOLERANCE = 32 * 1024
    DEFAULT_MAX_RANGES_PER_REQUEST = 30
    DEFAULT_MIN_TRANSFER_SPEED_BYTES = 50 * 1024
    DEFAULT_BASE_TIMEOUT_SECONDS = 30
    DEFAULT_MAX_TIMEOUT_SECONDS = 10 * 60
    CONTENT_RANGE_REGEX = re.compile(r"^bytes\s+(\d+)-(\d+)/(?:\d+|\*)$", re.I)

    def __init__(
        self,
        file: Optional[StrPath],
        path: StrPath,
        bundle_url: str = "https://lol.dyn.riotcdn.net/channels/public/bundles/",
        concurrency_limit: int = 50,
        max_retries: int = RETRY_LIMIT,
    ):
        """初始化 manifest 对象并完成解析。

        Args:
            file: 本地 manifest 路径或远程 manifest URL。
            path: 输出目录。
            bundle_url: bundle 基础 URL。
            concurrency_limit: 默认 bundle 并发数。
            max_retries: 单个 bundle 任务最大重试次数。

        Raises:
            ValueError: file 为空或路径无效时抛出。
        """
        self.file = file
        self.bundles: Iterable[PatcherBundle] = {}
        self.chunks: Dict[int, PatcherChunk] = {}
        self.flags: Dict[int, str] = {}
        self.files: Dict[str, PatcherFile] = {}

        self.path = path
        self.bundle_url = bundle_url
        self.concurrency_limit = concurrency_limit
        self.gap_tolerance = self.DEFAULT_GAP_TOLERANCE
        self.max_ranges_per_request = self.DEFAULT_MAX_RANGES_PER_REQUEST
        self.max_retries = max(1, max_retries)

        # file 不能为空
        if not file:
            raise ValueError("file can't be empty")

        parsed_url = urlparse(file)
        if parsed_url.scheme and parsed_url.netloc:
            self.parse_rman(io.BytesIO(http_get_bytes(file)))
        elif os.path.isfile(file) and os.path.exists(file):
            with open(file, "rb") as f:
                self.parse_rman(f)
        else:
            # 文件错误
            raise ValueError("file error")

    def _file_output(self, file: PatcherFile) -> str:
        return os.path.join(self.path, file.name)

    @staticmethod
    def _is_complete_file(file: PatcherFile, output: StrPath) -> bool:
        return os.path.isfile(output) and os.path.getsize(output) == file.size

    def _preallocate_file(self, file: PatcherFile):
        output = self._file_output(file)
        os.makedirs(os.path.dirname(output), exist_ok=True)
        with open(output, "wb") as f:
            f.truncate(file.size)

    def _build_global_task_map(self, files: List[PatcherFile]) -> Dict[int, List[GlobalChunkTask]]:
        chunk_index: Dict[int, GlobalChunkTask] = {}

        for file in files:
            file_offset = 0
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

        bundle_map: Dict[int, List[GlobalChunkTask]] = {}
        for task in chunk_index.values():
            bundle_id = task.chunk.bundle.bundle_id
            bundle_map.setdefault(bundle_id, []).append(task)

        for tasks in bundle_map.values():
            tasks.sort(key=lambda t: t.chunk.offset)
        return bundle_map

    @staticmethod
    def _merge_ranges(tasks: List[GlobalChunkTask], gap_tolerance: int) -> List[ChunkRange]:
        valid_tasks = [task for task in tasks if task.chunk.size > 0]
        if not valid_tasks:
            return []

        ranges: List[ChunkRange] = []
        start = valid_tasks[0].chunk.offset
        end = start + valid_tasks[0].chunk.size - 1
        current_tasks: List[GlobalChunkTask] = [valid_tasks[0]]

        for task in valid_tasks[1:]:
            task_start = task.chunk.offset
            task_end = task_start + task.chunk.size - 1
            gap = task_start - (end + 1)
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

    def _build_bundle_jobs(self, files: List[PatcherFile]) -> List[BundleJob]:
        bundle_map = self._build_global_task_map(files)
        jobs: List[BundleJob] = []

        for bundle_id, tasks in bundle_map.items():
            ranges = self._merge_ranges(tasks, self.gap_tolerance)
            if not ranges:
                continue
            for i in range(0, len(ranges), self.max_ranges_per_request):
                jobs.append(BundleJob(bundle_id=bundle_id, ranges=ranges[i : i + self.max_ranges_per_request]))

        return jobs

    @staticmethod
    def _build_range_header(ranges: List[ChunkRange]) -> str:
        return "bytes=" + ",".join(f"{chunk_range.start}-{chunk_range.end}" for chunk_range in ranges)

    @classmethod
    def _dynamic_request_timeout(cls, total_bytes: int) -> aiohttp.ClientTimeout:
        """按请求数据量计算动态超时。"""
        size_factor = total_bytes / float(cls.DEFAULT_MIN_TRANSFER_SPEED_BYTES)
        timeout_seconds = cls.DEFAULT_BASE_TIMEOUT_SECONDS + int(size_factor)
        timeout_seconds = min(timeout_seconds, cls.DEFAULT_MAX_TIMEOUT_SECONDS)
        timeout_seconds = max(timeout_seconds, cls.DEFAULT_BASE_TIMEOUT_SECONDS)
        return aiohttp.ClientTimeout(total=timeout_seconds, sock_connect=30, sock_read=None)

    @staticmethod
    def _hkdf_hash(chunk_data: bytes) -> int:
        """按 RMAN 规则计算 HKDF 派生哈希（uint64）。"""
        prk = hashlib.sha256(chunk_data).digest()
        buffer = hmac.new(prk, b"\x00\x00\x00\x01", hashlib.sha256).digest()
        result = int.from_bytes(buffer[:8], "little")
        for _ in range(31):
            buffer = hmac.new(prk, buffer, hashlib.sha256).digest()
            result ^= int.from_bytes(buffer[:8], "little")
        return result

    @staticmethod
    def _compute_chunk_hash(chunk_data: bytes, hash_type: int) -> Optional[int]:
        """按 hash_type 计算 chunk 哈希并返回 uint64。"""
        if hash_type == HASH_TYPE_SHA256:
            digest = hashlib.sha256(chunk_data).digest()
            return int.from_bytes(digest[:8], "little")
        if hash_type == HASH_TYPE_SHA512:
            digest = hashlib.sha512(chunk_data).digest()
            return int.from_bytes(digest[:8], "little")
        if hash_type == HASH_TYPE_HKDF:
            return PatcherManifest._hkdf_hash(chunk_data)
        if hash_type == HASH_TYPE_BLAKE3:
            if blake3 is None:
                raise DecompressError("缺少 blake3 依赖，无法校验 Blake3 Chunk 哈希")
            digest = blake3.blake3(chunk_data).digest()
            return int.from_bytes(digest[:8], "little")
        if hash_type == 0:
            return None
        raise DecompressError(f"不支持的 Chunk 哈希类型: {hash_type}")

    def _validate_chunk_hash(self, chunk_data: bytes, chunk_id: int, hash_type: int):
        """校验解压后的 chunk 数据哈希是否与 chunk_id 一致。"""
        computed = self._compute_chunk_hash(chunk_data, hash_type)
        if computed is None:
            return
        if computed != chunk_id:
            raise DecompressError(
                f"Chunk 哈希校验失败: hash_type={hash_type}, computed={computed:016X}, expected={chunk_id:016X}"
            )

    @staticmethod
    def _extract_ranges_from_full_body(payload: bytes, ranges: List[ChunkRange], bundle_id: int) -> List[bytes]:
        outputs: List[bytes] = []
        payload_len = len(payload)
        for chunk_range in ranges:
            if payload_len < chunk_range.end + 1:
                raise DownloadError(
                    f"完整内容不足以切片range: bundle_id={bundle_id}, range={chunk_range.start}-{chunk_range.end}, "
                    f"payload_len={payload_len}"
                )
            outputs.append(payload[chunk_range.start : chunk_range.end + 1])
        return outputs

    async def _parse_multipart_response(
        self,
        response: aiohttp.ClientResponse,
        ranges: List[ChunkRange],
        bundle_id: int,
    ) -> List[bytes]:
        reader = aiohttp.MultipartReader.from_response(response)
        index_by_range = {(chunk_range.start, chunk_range.end): idx for idx, chunk_range in enumerate(ranges)}
        mapped_parts: List[Optional[bytes]] = [None] * len(ranges)
        fallback_parts: List[bytes] = []

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

        for idx, value in enumerate(mapped_parts):
            if value is None:
                if not fallback_parts:
                    raise DownloadError(
                        f"multipart段数不足: bundle_id={bundle_id}, expected={len(ranges)}"
                    )
                mapped_parts[idx] = fallback_parts.pop(0)

        if fallback_parts:
            raise DownloadError(
                f"multipart段数过多: bundle_id={bundle_id}, expected={len(ranges)}, actual>{len(ranges)}"
            )

        return [part for part in mapped_parts if part is not None]

    async def _fetch_ranges_data(
        self,
        session: aiohttp.ClientSession,
        bundle_id: int,
        ranges: List[ChunkRange],
    ) -> List[bytes]:
        if not ranges:
            return []

        url = urljoin(self.bundle_url, f"{bundle_id:016X}.bundle")
        range_header = self._build_range_header(ranges)
        total_bytes = sum(chunk_range.end - chunk_range.start + 1 for chunk_range in ranges)
        request_timeout = self._dynamic_request_timeout(total_bytes)

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
                    range_payloads = self._extract_ranges_from_full_body(payload, ranges, bundle_id)
                else:
                    content_type = response.headers.get(aiohttp.hdrs.CONTENT_TYPE, "").lower()
                    if content_type.startswith("multipart/"):
                        range_payloads = await self._parse_multipart_response(response, ranges, bundle_id)
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

                for chunk_range, payload in zip(ranges, range_payloads):
                    expected_size = chunk_range.end - chunk_range.start + 1
                    if len(payload) != expected_size:
                        raise DownloadError(
                            f"下载range失败: bundle_id={bundle_id}, range={chunk_range.start}-{chunk_range.end}, "
                            f"actual={len(payload)}, expected={expected_size}"
                        )
                return range_payloads
        except (aiohttp.ClientError, asyncio.TimeoutError, DownloadError) as e:
            raise DownloadError(f"下载 bundle {bundle_id:016X} ranges 失败: {e}") from e

    async def _process_bundle_job(
        self,
        session: aiohttp.ClientSession,
        job: BundleJob,
        file_pool: FileHandlePool,
    ):
        range_payloads = await self._fetch_ranges_data(
            session=session,
            bundle_id=job.bundle_id,
            ranges=job.ranges,
        )

        for chunk_range, range_data in zip(job.ranges, range_payloads):

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
                except pyzstd.ZstdError as e:
                    raise DecompressError(
                        f"解压chunk失败: chunk_id={chunk.chunk_id}, bundle_id={chunk.bundle.bundle_id}"
                    ) from e

                verified_hash_keys = set()
                for verify_target in task.targets:
                    verify_key = (verify_target.chunk_id, verify_target.hash_type)
                    if verify_key in verified_hash_keys:
                        continue
                    self._validate_chunk_hash(
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

                    output = self._file_output(target.file)
                    await asyncio.to_thread(file_pool.write_at, output, data, target.file_offset)

    async def _run_bundle_job_with_retry(
        self,
        session: aiohttp.ClientSession,
        job: BundleJob,
        file_pool: FileHandlePool,
    ):
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                await self._process_bundle_job(session=session, job=job, file_pool=file_pool)
                return
            except (DownloadError, DecompressError, OSError) as e:
                last_error = e
                if attempt == self.max_retries - 1:
                    break
                await asyncio.sleep(attempt + 1)
        raise DownloadError(
            f"bundle任务失败: bundle_id={job.bundle_id}, retries={self.max_retries}, error={last_error}"
        )

    def filter_files(
        self, pattern: Optional[str] = None, flag: Union[str, List[str], None] = None
    ) -> Iterable["PatcherFile"]:
        """
        使用提供的名称模式和标志从清单中过滤文件。

        :param pattern: 用于匹配文件的名称模式。如果为None，则不应用名称过滤。
        :param flag: 用于匹配文件的标志字符串或标志字符串列表。如果为None，则不应用标志过滤。
        :return: 匹配提供的名称模式和标志字符串的PatcherFile对象的可迭代对象。
        """

        if isinstance(flag, str):
            flag = [flag]

        if not pattern and not flag:
            return self.files.values()

        # 生成匹配函数, 如果使用lambda会很简洁，但是E731：不建议使用 lambda 表达式
        # 简单说就是pattern 正则 匹配文件名，flag 匹配文件标志
        if pattern:
            name_regex = re.compile(pattern, re.I)

            def name_match(f):
                return bool(name_regex.search(f.name))

        else:

            def name_match(_):
                return True

        if flag:

            def flag_match(f):
                return f.flags is not None and any(flag_item in f.flags for flag_item in flag)

        else:

            def flag_match(_):
                return True

        def file_match(f):
            return name_match(f) and flag_match(f)

        return filter(file_match, self.files.values())

    async def download_files_concurrently(
        self,
        files: List[PatcherFile],
        concurrency_limit: Optional[int] = None,
        raise_on_error: bool = True,
    ) -> Tuple[bool, ...]:
        """全局并发下载多个文件。

        关键策略：
        1. 按 ChunkID 全局去重，再按 Bundle 分组。
        2. 同一 Bundle 执行 range 合并，减少请求数量。
        3. 下载后按写入目标扇出到多个文件，避免重复下载与重复解压。

        Args:
            files: 需要下载的文件列表。
            concurrency_limit: 覆盖默认并发；为 None 时使用实例配置。
            raise_on_error: 为 True 时只要存在失败 bundle 即抛出 DownloadBatchError。

        Returns:
            与输入顺序一致的结果元组，每个元素表示对应文件是否成功。

        Raises:
            DownloadBatchError: 存在失败 bundle 且 raise_on_error=True 时抛出。
        """
        def build_results(target_files: List[PatcherFile], failed_bundle_ids: Optional[set[int]] = None) -> Tuple[bool, ...]:
            failed_bundle_ids = failed_bundle_ids or set()
            results: List[bool] = []
            for target_file in target_files:
                if target_file.link:
                    results.append(True)
                    continue
                output = self._file_output(target_file)
                if not self._is_complete_file(target_file, output):
                    results.append(False)
                    continue
                has_failed_chunk = any(
                    chunk.bundle.bundle_id in failed_bundle_ids for chunk in target_file.chunks
                )
                results.append(not has_failed_chunk)
            return tuple(results)

        if not files:
            return tuple()

        # 保持输入顺序去重，避免重复统计同一文件
        seen_files: Dict[str, PatcherFile] = {}
        ordered_files: List[PatcherFile] = []
        for file in files:
            if file.name not in seen_files:
                seen_files[file.name] = file
                ordered_files.append(file)

        pending_files: List[PatcherFile] = []
        for file in ordered_files:
            if file.link:
                continue
            output = self._file_output(file)
            if not self._is_complete_file(file, output):
                self._preallocate_file(file)
                pending_files.append(file)

        # 全部已完成或均为link文件
        if not pending_files:
            return build_results(files)

        jobs = self._build_bundle_jobs(pending_files)
        if not jobs:
            return build_results(files)

        effective_concurrency = concurrency_limit if concurrency_limit is not None else self.concurrency_limit
        worker_count = max(1, min(effective_concurrency, len(jobs)))
        connector = aiohttp.TCPConnector(
            limit=max(worker_count * 4, 16),
            limit_per_host=max(worker_count * 4, 16),
        )
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=None)
        file_pool = FileHandlePool(max_handles=max(worker_count * 8, 256))

        queue: asyncio.Queue = asyncio.Queue()
        for job in jobs:
            queue.put_nowait(job)

        errors: List[BundleJobFailure] = []
        error_lock = asyncio.Lock()

        async def worker():
            while True:
                try:
                    job = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                try:
                    await self._run_bundle_job_with_retry(session=session, job=job, file_pool=file_pool)
                except Exception as exc:
                    async with error_lock:
                        errors.append(BundleJobFailure(bundle_id=job.bundle_id, error=exc))
                finally:
                    queue.task_done()

        try:
            async with aiohttp.ClientSession(connector=connector, timeout=timeout, auto_decompress=False) as session:
                workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
                await queue.join()
                await asyncio.gather(*workers)
        finally:
            await asyncio.to_thread(file_pool.close)

        if errors:
            if raise_on_error:
                raise DownloadBatchError(errors)
            for failure in errors:
                logger.error(f"bundle下载失败: {failure.bundle_id:016X}, error={failure.error}")
            return build_results(files, failed_bundle_ids={failure.bundle_id for failure in errors})

        return build_results(files)

    def parse_rman(self, f: BinaryIO):
        parser = BinaryParser(f)

        magic, version_major, version_minor = parser.unpack("<4sBB")
        if magic != b"RMAN":
            raise ValueError("invalid magic code")
        if (version_major, version_minor) not in ((2, 0), (2, 1)):
            raise ValueError(f"unsupported RMAN version: {version_major}.{version_minor}")

        flags, offset, length, _manifest_id, _body_length = parser.unpack("<HLLQL")
        assert flags & (1 << 9)  # other flags not handled
        assert offset == parser.tell()

        f = io.BytesIO(pyzstd.decompress(parser.raw(length)))
        return self.parse_body(f)

    def parse_body(self, f: BinaryIO):
        parser = BinaryParser(f)

        # header (unknown values, skip it)
        (n,) = parser.unpack("<l")
        parser.skip(n)

        # offsets to tables (convert to absolute)
        offsets_base = parser.tell()
        offsets = list(offsets_base + 4 * i + v for i, v in enumerate(parser.unpack("<6l")))

        parser.seek(offsets[0])
        self.bundles = list(self._parse_table(parser, self._parse_bundle))

        parser.seek(offsets[1])
        self.flags = dict(self._parse_table(parser, self._parse_flag))

        # build a list of chunks, indexed by ID
        self.chunks = {chunk.chunk_id: chunk for bundle in self.bundles for chunk in bundle.chunks}

        parser.seek(offsets[2])
        file_entries = list(self._parse_table(parser, self._parse_file_entry))
        parser.seek(offsets[3])
        directories = {did: (name, parent) for name, did, parent in self._parse_table(parser, self._parse_directory)}
        parameter_hash_types: List[int] = []
        if len(offsets) > 5 and offsets[5] > 0:
            parser.seek(offsets[5])
            parameter_hash_types = list(self._parse_table(parser, self._parse_parameter))

        # merge files and directory data
        self.files = {}
        for name, link, flag_ids, dir_id, filesize, chunk_ids, param_index in file_entries:
            while dir_id is not None:
                dir_name, dir_id = directories[dir_id]
                name = f"{dir_name}/{name}"
            if flag_ids is not None:
                flags = [self.flags[i] for i in flag_ids]
            else:
                flags = None
            file_chunks = [self.chunks[chunk_id] for chunk_id in chunk_ids]
            hash_type = 0
            if param_index is not None and 0 <= param_index < len(parameter_hash_types):
                hash_type = parameter_hash_types[param_index]
            chunk_hash_types = {chunk_id: hash_type for chunk_id in chunk_ids}
            self.files[name] = PatcherFile(
                name,
                filesize,
                link,
                flags,
                file_chunks,
                self,
                chunk_hash_types=chunk_hash_types,
            )

        # note: last two tables are unresolved

    @staticmethod
    def _parse_table(parser, entry_parser):
        (count,) = parser.unpack("<l")

        for _ in range(count):
            pos = parser.tell()
            (offset,) = parser.unpack("<l")
            parser.seek(pos + offset)
            yield entry_parser(parser)
            parser.seek(pos + 4)

    @classmethod
    def _parse_bundle(cls, parser):
        """Parse a bundle entry"""

        def parse_chunklist(_):
            fields = cls._parse_field_table(parser, (
                ('chunk_id', '<Q'),
                ('compressed_size', '<L'),
                ('uncompressed_size', '<L'),
            ))
            return fields['chunk_id'], fields['compressed_size'], fields['uncompressed_size']

        fields = cls._parse_field_table(parser, (
            ('bundle_id', '<Q'),
            ('chunks_offset', 'offset'),
        ))

        bundle = PatcherBundle(fields['bundle_id'])
        parser.seek(fields['chunks_offset'])
        for (chunk_id, compressed_size, uncompressed_size) in cls._parse_table(parser, parse_chunklist):
            bundle.add_chunk(chunk_id, compressed_size, uncompressed_size)

        return bundle

    @staticmethod
    def _parse_flag(parser):
        parser.skip(4)  # skip offset table offset
        flag_id, offset, = parser.unpack('<xxxBl')
        parser.skip(offset - 4)
        return flag_id, parser.unpack_string()

    @classmethod
    def _parse_file_entry(cls, parser):
        """Parse a file entry
        (name, link, flag_ids, directory_id, filesize, chunk_ids, param_index)
        """
        fields = cls._parse_field_table(parser, (
            ('file_id', '<Q'),
            ('directory_id', '<Q'),
            ('file_size', '<L'),
            ('name', 'str'),
            ('flags', '<Q'),
            None,
            None,
            ('chunks', 'offset'),
            None,
            ('link', 'str'),
            None,
            ('param_index', '<B'),
            None,
        ))

        flag_mask = fields['flags']
        if flag_mask:
            flag_ids = [i+1 for i in range(64) if flag_mask & (1 << i)]
        else:
            flag_ids = None

        parser.seek(fields['chunks'])
        chunk_count, = parser.unpack('<L')  # _ == 0
        chunk_ids = list(parser.unpack(f'<{chunk_count}Q'))

        return (
            fields['name'],
            fields['link'],
            flag_ids,
            fields['directory_id'],
            fields['file_size'],
            chunk_ids,
            fields['param_index'] or 0,
        )

    @classmethod
    def _parse_parameter(cls, parser):
        fields = cls._parse_field_table(parser, (
            None,
            ('hash_type', '<B'),
            ('min_chunk_size', '<L'),
            ('max_chunk_size', '<L'),
            ('max_uncompressed_size', '<L'),
        ))
        return fields['hash_type'] or 0

    @classmethod
    def _parse_directory(cls, parser):
        """Parse a directory entry
        (name, directory_id, parent_id)
        """
        fields = cls._parse_field_table(parser, (
            ('directory_id', '<Q'),
            ('parent_id', '<Q'),
            ('name', 'str'),
        ))
        return fields['name'], fields['directory_id'], fields['parent_id']

    @staticmethod
    def _parse_field_table(parser, fields):
        entry_pos = parser.tell()
        fields_pos = entry_pos - parser.unpack('<l')[0]
        output = {}
        parser.seek(fields_pos)
        vtable_size = parser.unpack('<H')[0]
        parser.skip(2)  # object size
        noffsets = (vtable_size - 4) // 2
        offsets = parser.unpack(f'<{noffsets}H')

        for i, field in enumerate(fields):
            if field is None:
                continue
            name, fmt = field
            if i >= noffsets or (offset := offsets[i]) == 0 or fmt is None:
                value = None
            else:
                pos = entry_pos + offset
                parser.seek(pos)
                if fmt == 'offset':
                    value = pos + parser.unpack('<l')[0]
                elif fmt == 'str':
                    value = parser.unpack('<l')[0]
                    parser.seek(pos + value)
                    value = parser.unpack_string()
                else:
                    value = parser.unpack(fmt)[0]
            output[name] = value
        return output
