# -*- coding: utf-8 -*-
# @Author  : Virace
# @Email   : Virace@aliyun.com
# @Site    : x-item.com
# @Software: Pycharm
# @Create  : 2024/3/12 22:46
# @Update  : 2025/3/11 16:17
# @Detail  : manifest.py

import asyncio
import hashlib
import io
import os
import os.path
import re
import struct
from typing import BinaryIO, Iterable, Optional, Tuple
from typing import Dict, List, Union
from urllib.parse import urljoin, urlparse

import aiohttp
import pyzstd
import requests
from loguru import logger

RETRY_LIMIT = 5

StrPath = Union[str, "os.PathLike[str]"]


class DownloadError(Exception):
    pass


class DecompressError(Exception):
    pass


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


class PatcherFile:
    def __init__(
        self,
        name: str,
        size: int,
        link: str,
        flags: Optional[List[str]],
        chunks: List[PatcherChunk],
        manifest: "PatcherManifest",
    ):
        """
        Patch file, 可以直接调用download_file方法下载文件, 注意是异步方法

        hexdigest() ,并不是文件的哈希,而是由文件的chunks的chunk_id组成的哈希, 可以再未下载时判断文件是否相同
        :param name:
        :param size:
        :param link:
        :param flags:
        :param chunks:
        """
        self.name: str = name
        self.size: int = size
        self.link: str = link
        self.flags: Optional[List[str]] = flags

        self.chunks: List[PatcherChunk] = chunks
        self.manifest: "PatcherManifest" = manifest

        self.chunk_cache = {}
        self.lock = asyncio.Lock()

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

    async def _download_chunks(self, chunks: List[PatcherChunk], concurrency_limit: int):
        """
        下载一系列的chunks并将它们保存到缓存中

        :param chunks: 需要下载的chunks列表
        :type chunks: List[PatcherChunk]
        """
        # 自定义并发，防止遇到网站审计
        connector = aiohttp.TCPConnector(limit=concurrency_limit)
        async with aiohttp.ClientSession(connector=connector) as session:
            tasks = [self._download_chunk(session, chunk, chunk.offset, chunk.size) for chunk in chunks]
            await asyncio.gather(*tasks)

    async def _download_chunk(
        self,
        session: aiohttp.ClientSession,
        chunk: PatcherChunk,
        offset: int,
        size: int,
    ):
        """
        下载一个chunk并返回其内容

        :param session: aiohttp的ClientSession实例，用于发送HTTP请求
        :param chunk: 需要下载的chunk
        :param offset: chunk在bundle中的偏移量
        :param size: chunk的大小
        :return: 下载的chunk的内容
        """
        async with self.lock:
            if chunk.chunk_id in self.chunk_cache:
                return self.chunk_cache[chunk.chunk_id]

        url = urljoin(self.manifest.bundle_url, f"{chunk.bundle.bundle_id:016X}.bundle")
        for i in range(RETRY_LIMIT):
            try:
                async with session.get(url, headers={"Range": f"bytes={offset}-{offset + size - 1}"}) as res:
                    res.raise_for_status()
                    content = await res.read()
                    if len(content) != size:
                        raise DownloadError(
                            f"下载的chunk {chunk.chunk_id}失败，得到了 {len(content)} 字节，期望 {size} 字节，bundle_id为 {chunk.bundle.bundle_id}"
                        )
                    break
            except aiohttp.ClientError as e:
                if i == RETRY_LIMIT - 1:
                    raise DownloadError(
                        f"在 {RETRY_LIMIT} 次尝试后，下载chunk {chunk.chunk_id}失败，bundle_id为 {chunk.bundle.bundle_id}"
                    ) from e
                await asyncio.sleep(5)

        try:
            data = pyzstd.decompress(content)
        except pyzstd.ZstdError as e:
            raise DecompressError(f"解压缩chunk {chunk.chunk_id}时出错，bundle_id为 {chunk.bundle.bundle_id}") from e

        async with self.lock:
            self.chunk_cache[chunk.chunk_id] = data

        return data

    def _verify_file(self, path: StrPath) -> bool:
        """
        检查文件是否与chunks匹配

        :param path: 文件路径
        :return: 如果文件需要下载，则返回True；否则，返回False
        """

        if os.path.exists(path) and os.path.getsize(path) == sum(chunk.target_size for chunk in self.chunks):
            logger.info(f"{self.name}，校验通过")
            return True
        return False

    async def download_file(self, path: StrPath, concurrency_limit: Optional[int] = None) -> bool:
        """
        下载一个文件并将其保存到磁盘
        :param path: 保存文件的路径
        :param concurrency_limit: 并发数
        """
        output = os.path.join(path, self.name)

        if self._verify_file(output):
            return True

        os.makedirs(os.path.dirname(output), exist_ok=True)

        try:
            await self._download_chunks(self.chunks, concurrency_limit or self.manifest.concurrency_limit)
        except (DownloadError, DecompressError) as e:
            logger.error(f"下载文件 {self.name} 时出错: {str(e)}")
            return False

        with open(output, "wb+") as f:
            for chunk in self.chunks:
                f.write(self.chunk_cache[chunk.chunk_id])

        status = self._verify_file(path)
        logger.info(f"下载文件 {self.name} 完成, 状态: {status}")
        return status

    def download_chunk(self, chunk: "PatcherChunk") -> bytes:
        """
        下载一个chunk并返回其解压缩后的内容（同步方法）。

        :param chunk: 需要下载的PatcherChunk对象。
        :return: 解压缩后的chunk内容字节数据。
        :raises DownloadError: 在达到重试限制后仍然无法成功下载时抛出。
        :raises DecompressError: 在解压缩过程中发生错误时抛出。
        """
        if chunk.chunk_id in self.chunk_cache:
            return self.chunk_cache[chunk.chunk_id]

        url = urljoin(self.manifest.bundle_url, f"{chunk.bundle.bundle_id:016X}.bundle")
        content = b""
        for attempt in range(RETRY_LIMIT):
            try:
                headers = {"Range": f"bytes={chunk.offset}-{chunk.offset + chunk.size - 1}"}
                response = requests.get(url, headers=headers)
                response.raise_for_status()
                content = response.content

                if len(content) != chunk.size:
                    raise DownloadError(
                        f"下载的chunk {chunk.chunk_id}失败，获取到 {len(content)} 字节，期望 {chunk.size} 字节，"
                        f"bundle_id为 {chunk.bundle.bundle_id}"
                    )
                break
            except requests.RequestException as e:
                if attempt == RETRY_LIMIT - 1:
                    raise DownloadError(
                        f"在 {RETRY_LIMIT} 次尝试后，下载chunk {chunk.chunk_id}失败，bundle_id为 {chunk.bundle.bundle_id}"
                    ) from e

        try:
            decompressed_data = pyzstd.decompress(content)
        except pyzstd.ZstdError as e:
            raise DecompressError(f"解压缩chunk {chunk.chunk_id}时出错，bundle_id为 {chunk.bundle.bundle_id}") from e

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
    def __init__(
        self,
        file: Optional[StrPath],
        path: StrPath,
        bundle_url: str = "https://lol.dyn.riotcdn.net/channels/public/bundles/",
        concurrency_limit: int = 50,
    ):
        """

        :param file:
        :param bundle_url:
        :param concurrency_limit:
        """
        self.file = file
        self.bundles: Iterable[PatcherBundle] = {}
        self.chunks: Dict[int, PatcherChunk] = {}
        self.flags: Dict[int, str] = {}
        self.files: Dict[str, PatcherFile] = {}

        self.path = path
        self.bundle_url = bundle_url
        self.concurrency_limit = concurrency_limit

        # file 不能为空
        if not file:
            raise ValueError("file can't be empty")

        parsed_url = urlparse(file)
        if parsed_url.scheme and parsed_url.netloc:
            res = requests.get(file)
            self.parse_rman(io.BytesIO(res.content))
        elif os.path.isfile(file) and os.path.exists(file):
            with open(file, "rb") as f:
                self.parse_rman(f)
        else:
            # 文件错误
            raise ValueError("file error")

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

    async def download_files_concurrently(self, files: List[PatcherFile], concurrency_limit: int = 10) -> Tuple[bool]:
        """
        并发下载多个文件, 并发数别设置太大，会被限制

        :param files: 需要下载的文件列表，每个元素都是一个PatcherFile实例
        :param concurrency_limit: 并发下载任务的数量限制，默认为10
        :return: 一个元组，包含所有下载结果的布尔值
        """
        # 创建一个信号量，限制并发下载任务的数量
        sem = asyncio.Semaphore(concurrency_limit)

        # 创建一个包含所有下载任务的列表
        tasks = []
        for file in files:
            # 使用信号量限制并发下载任务的数量
            async with sem:
                tasks.append(file.download_file(path=self.path, concurrency_limit=50))

        # 使用 asyncio.gather 并发运行所有下载任务
        return await asyncio.gather(*tasks)

    def parse_rman(self, f: BinaryIO):
        parser = BinaryParser(f)

        magic, version_major, version_minor = parser.unpack("<4sBB")
        if magic != b"RMAN":
            raise ValueError("invalid magic code")
        if (version_major, version_minor) != (2, 0):
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

        # merge files and directory data
        self.files = {}
        for name, link, flag_ids, dir_id, filesize, chunk_ids in file_entries:
            while dir_id is not None:
                dir_name, dir_id = directories[dir_id]
                name = f"{dir_name}/{name}"
            if flag_ids is not None:
                flags = [self.flags[i] for i in flag_ids]
            else:
                flags = None
            file_chunks = [self.chunks[chunk_id] for chunk_id in chunk_ids]
            self.files[name] = PatcherFile(name, filesize, link, flags, file_chunks, self)

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
        (name, link, flag_ids, directory_id, filesize, chunk_ids)
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
            None,
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

        return fields['name'], fields['link'], flag_ids, fields['directory_id'], fields['file_size'], chunk_ids

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
        nfields = len(fields)
        output = {}
        parser.seek(fields_pos)
        parser.skip(2) # vtable size
        parser.skip(2) # object size
        for _, field, offset in zip(range(nfields), fields, parser.unpack(f'<{nfields}H')):
            if field is None:
                continue
            name, fmt = field
            if offset == 0 or fmt is None:
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