"""Riot manifest 解析与并发下载核心实现.

该模块聚焦 manifest 解析与数据模型；
下载调度由 `riotmanifest.downloader` 子模块负责。
"""

from __future__ import annotations

import hashlib
import io
import os
import re
from collections.abc import Iterable
from typing import BinaryIO, Union
from urllib.parse import urljoin, urlparse

import pyzstd
from loguru import logger

from riotmanifest.core.binary_parser import BinaryParser
from riotmanifest.core.chunk_hash import (
    validate_chunk_hash,
)
from riotmanifest.core.errors import (
    DecompressError,
    DownloadError,
)
from riotmanifest.downloader.scheduler import DownloadScheduler
from riotmanifest.utils.http_client import HttpClientError, http_get_bytes

RETRY_LIMIT = 5

StrPath = Union[str, "os.PathLike[str]"]


class PatcherChunk:
    """描述 bundle 内单个 chunk 元数据."""

    def __init__(
        self,
        chunk_id: int,
        bundle: PatcherBundle,
        offset: int,
        size: int,
        target_size: int,
    ):
        """初始化 chunk 元数据.

        Args:
            chunk_id: chunk 唯一标识。
            bundle: 所属 bundle。
            offset: chunk 在 bundle 内偏移。
            size: chunk 压缩后大小。
            target_size: chunk 解压后大小。
        """
        self.chunk_id: int = chunk_id
        self.bundle: PatcherBundle = bundle
        self.offset: int = offset
        self.size: int = size
        self.target_size: int = target_size

    def __hash__(self):
        """返回基于 chunk_id 的哈希值."""
        return self.chunk_id


class PatcherBundle:
    """描述一个 bundle 与其 chunk 列表."""

    def __init__(self, bundle_id: int):
        """初始化 bundle 对象.

        Args:
            bundle_id: bundle 唯一标识。
        """
        self.bundle_id: int = bundle_id
        self.chunks: list[PatcherChunk] = []

    def add_chunk(self, chunk_id: int, size: int, target_size: int):
        """向 bundle 追加一个 chunk 定义."""
        try:
            last_chunk = self.chunks[-1]
            offset = last_chunk.offset + last_chunk.size
        except IndexError:
            offset = 0
        self.chunks.append(PatcherChunk(chunk_id, self, offset, size, target_size))


class PatcherFile:
    """Manifest 中文件元数据及下载接口."""

    def __init__(
        self,
        name: str,
        size: int,
        link: str,
        flags: list[str] | None,
        chunks: list[PatcherChunk],
        manifest: PatcherManifest,
        chunk_hash_types: dict[int, int] | None = None,
    ):
        """初始化补丁文件对象.

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
        self.flags: list[str] | None = flags

        self.chunks: list[PatcherChunk] = chunks
        self.manifest: PatcherManifest = manifest
        self.chunk_hash_types: dict[int, int] = chunk_hash_types or {}

        self.chunk_cache: dict[int, bytes] = {}

    def hexdigest(self):
        """Compute a hash unique for this file content."""
        m = hashlib.sha1()
        for chunk in self.chunks:
            m.update(b"%016X" % chunk.chunk_id)
        return m.hexdigest()

    @staticmethod
    def langs_predicate(langs):
        """Return a predicate function for a locale filtering parameter."""
        if langs is False:
            # 约定 locale flag 形如 `zh_CN`，仅保留非语言文件。
            return lambda f: f.flags is None or not any("_" in f and len(f) == 5 for f in f.flags)
        if langs is True:
            return lambda f: True

        lang = langs.lower()
        return lambda f: f.flags is not None and any(f.lower() == lang for f in f.flags)

    def _verify_file(self, path: StrPath) -> bool:
        """按文件大小进行快速校验."""
        if os.path.isfile(path) and os.path.getsize(path) == self.size:
            logger.info(f"{self.name}，校验通过")
            return True
        return False

    async def download_file(self, path: StrPath, concurrency_limit: int | None = None) -> bool:
        """下载单个文件（委托给 Manifest 全局调度器）."""
        self.manifest.path = path
        results = await self.manifest.download_files_concurrently(
            [self],
            concurrency_limit=concurrency_limit,
        )
        return bool(results and results[0])

    def download_chunk(self, chunk: PatcherChunk) -> bytes:
        """下载并解压单个 chunk（同步方法）."""
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
            except HttpClientError as exc:
                if attempt == RETRY_LIMIT - 1:
                    raise DownloadError(
                        f"在 {RETRY_LIMIT} 次尝试后，下载chunk {chunk.chunk_id}失败，bundle_id为 {chunk.bundle.bundle_id}"
                    ) from exc

        try:
            decompressed_data = pyzstd.decompress(content)
        except pyzstd.ZstdError as exc:
            raise DecompressError(f"解压缩chunk {chunk.chunk_id}时出错，bundle_id为 {chunk.bundle.bundle_id}") from exc

        hash_type = self.chunk_hash_types.get(chunk.chunk_id, 0)
        self.manifest.validate_chunk_hash(
            chunk_data=decompressed_data,
            chunk_id=chunk.chunk_id,
            hash_type=hash_type,
        )

        self.chunk_cache[chunk.chunk_id] = decompressed_data
        return decompressed_data

    def download_chunks(self, chunks: list[PatcherChunk]) -> bytes:
        """下载多个 chunk 并拼接为文件字节串."""
        combined_data = b""
        for chunk in chunks:
            combined_data += self.download_chunk(chunk)
        return combined_data


class PatcherManifest:
    """Manifest 解析、索引构建与下载调度入口."""

    DEFAULT_GAP_TOLERANCE = 32 * 1024
    DEFAULT_MAX_RANGES_PER_REQUEST = 30
    DEFAULT_MIN_TRANSFER_SPEED_BYTES = 50 * 1024
    DEFAULT_BASE_TIMEOUT_SECONDS = 30
    DEFAULT_MAX_TIMEOUT_SECONDS = 10 * 60
    def __init__(
        self,
        file: StrPath | None,
        path: StrPath,
        bundle_url: str = "https://lol.dyn.riotcdn.net/channels/public/bundles/",
        concurrency_limit: int = 16,
        max_retries: int = RETRY_LIMIT,
    ):
        """初始化 manifest 对象并完成解析.

        Args:
            file: 本地 manifest 路径或远程 manifest URL。
            path: 输出目录。
            bundle_url: bundle 基础 URL。
            concurrency_limit: 默认 bundle 并发数。
            max_retries: 单个 bundle 任务最大重试次数。

        Raises:
            ValueError: file 为空或路径无效时抛出。
        """
        self.file = os.fspath(file) if file else file
        self.bundles: Iterable[PatcherBundle] = {}
        self.chunks: dict[int, PatcherChunk] = {}
        self.flags: dict[int, str] = {}
        self.files: dict[str, PatcherFile] = {}

        self.path = path
        self.bundle_url = bundle_url
        self.concurrency_limit = concurrency_limit
        self.gap_tolerance = self.DEFAULT_GAP_TOLERANCE
        self.max_ranges_per_request = self.DEFAULT_MAX_RANGES_PER_REQUEST
        self.max_retries = max(1, max_retries)
        self.downloader = DownloadScheduler(self)

        if not file:
            raise ValueError("file can't be empty")

        file_ref = os.fspath(file)
        self.file = file_ref
        parsed_url = urlparse(file_ref)

        if parsed_url.scheme and parsed_url.netloc:
            self.parse_rman(io.BytesIO(http_get_bytes(file_ref)))
        elif os.path.isfile(file_ref):
            with open(file_ref, "rb") as f:
                self.parse_rman(f)
        else:
            raise ValueError("file error")

    def file_output(self, file: PatcherFile) -> str:
        """返回目标文件的绝对输出路径."""
        return os.path.join(self.path, file.name)

    @staticmethod
    def is_complete_file(file: PatcherFile, output: StrPath) -> bool:
        """判断本地文件是否已完整下载（按大小快速判定）."""
        return os.path.isfile(output) and os.path.getsize(output) == file.size

    def preallocate_file(self, file: PatcherFile):
        """预分配目标文件，提前占位避免并发写入时多次创建."""
        output = self.file_output(file)
        os.makedirs(os.path.dirname(output), exist_ok=True)
        with open(output, "wb") as f:
            f.truncate(file.size)

    def validate_chunk_hash(self, chunk_data: bytes, chunk_id: int, hash_type: int) -> None:
        """校验解压后的 chunk 数据哈希是否与 chunk_id 一致."""
        validate_chunk_hash(chunk_data=chunk_data, chunk_id=chunk_id, hash_type=hash_type)

    def filter_files(self, pattern: str | None = None, flag: str | list[str] | None = None) -> Iterable[PatcherFile]:
        """按文件名正则与 flag 过滤 manifest 文件项.

        Args:
            pattern: 文件名匹配正则，不传则不按名称过滤。
            flag: 目标 flag 字符串或字符串列表，不传则不按 flag 过滤。

        Returns:
            满足条件的文件迭代器。
        """
        if isinstance(flag, str):
            flag = [flag]

        if not pattern and not flag:
            return self.files.values()

        if pattern:
            name_regex = re.compile(pattern, re.I)

            def name_match(file_item: PatcherFile) -> bool:
                return bool(name_regex.search(file_item.name))

        else:

            def name_match(_: PatcherFile) -> bool:
                return True

        if flag:

            def flag_match(file_item: PatcherFile) -> bool:
                return file_item.flags is not None and any(flag_item in file_item.flags for flag_item in flag)

        else:

            def flag_match(_: PatcherFile) -> bool:
                return True

        return filter(lambda file_item: name_match(file_item) and flag_match(file_item), self.files.values())

    async def download_files_concurrently(
        self,
        files: list[PatcherFile],
        concurrency_limit: int | None = None,
        raise_on_error: bool = True,
    ) -> tuple[bool, ...]:
        """并发下载多个文件（下载调度入口）."""
        return await self.downloader.download_files_concurrently(files, concurrency_limit, raise_on_error)

    def parse_rman(self, f: BinaryIO):
        """解析 RMAN 头部并进入主体解析流程."""
        parser = BinaryParser(f)

        magic, version_major, version_minor = parser.unpack("<4sBB")
        if magic != b"RMAN":
            raise ValueError("invalid magic code")
        if (version_major, version_minor) not in ((2, 0), (2, 1)):
            raise ValueError(f"unsupported RMAN version: {version_major}.{version_minor}")

        flags, offset, length, _manifest_id, _body_length = parser.unpack("<HLLQL")
        if not flags & (1 << 9):
            raise ValueError(f"unsupported RMAN flags: {flags:#06x}")
        if offset != parser.tell():
            raise ValueError(f"invalid RMAN body offset: expected={parser.tell()}, got={offset}")

        body_stream = io.BytesIO(pyzstd.decompress(parser.raw(length)))
        return self.parse_body(body_stream)

    def parse_body(self, f: BinaryIO):
        """解析 manifest 主体并构建 bundle/chunk/file 索引."""
        parser = BinaryParser(f)

        # header 为未知扩展段，按长度直接跳过。
        (header_len,) = parser.unpack("<l")
        parser.skip(header_len)

        # 六张核心表的 offset 为“相对当前位置”的偏移，先统一转换为绝对偏移。
        offsets_base = parser.tell()
        offsets = [offsets_base + 4 * index + value for index, value in enumerate(parser.unpack("<6l"))]

        parser.seek(offsets[0])
        self.bundles = list(self._parse_table(parser, self._parse_bundle))

        parser.seek(offsets[1])
        self.flags = dict(self._parse_table(parser, self._parse_flag))

        # 先构建 chunk 索引，后续文件表会按 chunk_id 直接引用。
        self.chunks = {chunk.chunk_id: chunk for bundle in self.bundles for chunk in bundle.chunks}

        parser.seek(offsets[2])
        file_entries = list(self._parse_table(parser, self._parse_file_entry))

        parser.seek(offsets[3])
        directories = {
            directory_id: (name, parent)
            for name, directory_id, parent in self._parse_table(parser, self._parse_directory)
        }

        parameter_hash_types: list[int] = []
        if len(offsets) > 5 and offsets[5] > 0:
            parser.seek(offsets[5])
            parameter_hash_types = list(self._parse_table(parser, self._parse_parameter))

        self.files = {}
        for name, link, flag_ids, dir_id, file_size, chunk_ids, param_index in file_entries:
            # 目录链采用 parent 指针，逆向回溯拼接完整路径。
            while dir_id is not None:
                dir_name, dir_id = directories[dir_id]
                name = f"{dir_name}/{name}"

            flags = [self.flags[index] for index in flag_ids] if flag_ids is not None else None
            file_chunks = [self.chunks[chunk_id] for chunk_id in chunk_ids]

            hash_type = 0
            if param_index is not None and 0 <= param_index < len(parameter_hash_types):
                hash_type = parameter_hash_types[param_index]
            chunk_hash_types = {chunk_id: hash_type for chunk_id in chunk_ids}

            self.files[name] = PatcherFile(
                name=name,
                size=file_size,
                link=link,
                flags=flags,
                chunks=file_chunks,
                manifest=self,
                chunk_hash_types=chunk_hash_types,
            )

    @staticmethod
    def _parse_table(parser: BinaryParser, entry_parser):
        """按 offset-table 结构迭代解析表项."""
        (count,) = parser.unpack("<l")

        for _ in range(count):
            pos = parser.tell()
            (offset,) = parser.unpack("<l")
            parser.seek(pos + offset)
            yield entry_parser(parser)
            parser.seek(pos + 4)

    @classmethod
    def _parse_bundle(cls, parser: BinaryParser):
        """解析 bundle 条目并构建其 chunk 列表."""

        def parse_chunk_entry(_: BinaryParser):
            fields = cls._parse_field_table(
                parser,
                (
                    ("chunk_id", "<Q"),
                    ("compressed_size", "<L"),
                    ("uncompressed_size", "<L"),
                ),
            )
            return fields["chunk_id"], fields["compressed_size"], fields["uncompressed_size"]

        fields = cls._parse_field_table(
            parser,
            (
                ("bundle_id", "<Q"),
                ("chunks_offset", "offset"),
            ),
        )

        bundle = PatcherBundle(fields["bundle_id"])
        parser.seek(fields["chunks_offset"])
        for chunk_id, compressed_size, uncompressed_size in cls._parse_table(parser, parse_chunk_entry):
            bundle.add_chunk(chunk_id, compressed_size, uncompressed_size)
        return bundle

    @staticmethod
    def _parse_flag(parser: BinaryParser):
        """解析 flag 表项（flag_id -> flag_name）."""
        parser.skip(4)
        flag_id, offset = parser.unpack("<xxxBl")
        parser.skip(offset - 4)
        return flag_id, parser.unpack_string()

    @classmethod
    def _parse_file_entry(cls, parser: BinaryParser):
        """解析文件表项并返回标准化元组."""
        fields = cls._parse_field_table(
            parser,
            (
                ("file_id", "<Q"),
                ("directory_id", "<Q"),
                ("file_size", "<L"),
                ("name", "str"),
                ("flags", "<Q"),
                None,
                None,
                ("chunks", "offset"),
                None,
                ("link", "str"),
                None,
                ("param_index", "<B"),
                None,
            ),
        )

        flag_mask = fields["flags"]
        flag_ids = [i + 1 for i in range(64) if flag_mask & 1 << i] if flag_mask else None

        parser.seek(fields["chunks"])
        (chunk_count,) = parser.unpack("<L")
        chunk_ids = list(parser.unpack(f"<{chunk_count}Q"))

        return (
            fields["name"],
            fields["link"],
            flag_ids,
            fields["directory_id"],
            fields["file_size"],
            chunk_ids,
            fields["param_index"],
        )

    @classmethod
    def _parse_parameter(cls, parser: BinaryParser):
        """解析 parameter 表，返回 hash_type（缺省为 0）."""
        fields = cls._parse_field_table(
            parser,
            (
                None,
                ("hash_type", "<B"),
                ("min_chunk_size", "<L"),
                ("max_chunk_size", "<L"),
                ("max_uncompressed_size", "<L"),
            ),
        )
        return fields["hash_type"] or 0

    @classmethod
    def _parse_directory(cls, parser: BinaryParser):
        """解析目录表项并返回 (name, directory_id, parent_id)."""
        fields = cls._parse_field_table(
            parser,
            (
                ("directory_id", "<Q"),
                ("parent_id", "<Q"),
                ("name", "str"),
            ),
        )
        return fields["name"], fields["directory_id"], fields["parent_id"]

    @staticmethod
    def _parse_field_table(parser: BinaryParser, fields):
        """按 FlatBuffer vtable 语义解析字段集合.

        Args:
            parser: 二进制解析器。
            fields: 字段定义列表，元素为 `(name, fmt)` 或 `None`。

        Returns:
            字段名到值的映射，缺省字段返回 `None`。
        """
        entry_pos = parser.tell()
        fields_pos = entry_pos - parser.unpack("<l")[0]
        output: dict[str, object | None] = {}

        parser.seek(fields_pos)
        vtable_size = parser.unpack("<H")[0]
        parser.skip(2)
        noffsets = (vtable_size - 4) // 2
        offsets = parser.unpack(f"<{noffsets}H")

        for index, field_def in enumerate(fields):
            if field_def is None:
                continue

            name, fmt = field_def
            if index >= noffsets or (offset := offsets[index]) == 0 or fmt is None:
                output[name] = None
                continue

            pos = entry_pos + offset
            parser.seek(pos)

            if fmt == "offset":
                value = pos + parser.unpack("<l")[0]
            elif fmt == "str":
                rel_offset = parser.unpack("<l")[0]
                parser.seek(pos + rel_offset)
                value = parser.unpack_string()
            else:
                value = parser.unpack(fmt)[0]

            output[name] = value

        return output
