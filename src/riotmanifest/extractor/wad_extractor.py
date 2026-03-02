"""基于 manifest 按需提取 WAD 内部文件."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin

import pyzstd
from league_tools.formats import WAD, WadHeaderAnalyzer
from loguru import logger

from riotmanifest.extractor.cache import ChunkCache
from riotmanifest.manifest import (
    RETRY_LIMIT,
    DecompressError,
    DownloadError,
    PatcherChunk,
    PatcherFile,
    PatcherManifest,
)
from riotmanifest.utils.http_client import HttpClientError, http_get_bytes


class WADExtractor:
    """按需提取 WAD 内部文件，避免整包下载。."""

    V3_HEADER_MINI_SIZE = 4 + 268 + 4

    def __init__(
        self,
        manifest: PatcherManifest,
        bundle_url: str | None = None,
        cache_max_bytes: int = 128 * 1024 * 1024,
        cache_max_entries: int = 512,
        retry_limit: int = RETRY_LIMIT,
    ):
        """初始化 WAD 提取器。.

        Args:
            manifest: manifest 实例。
            bundle_url: bundle 基础 URL；为 None 时使用 manifest 内的默认值。
            cache_max_bytes: chunk 解压缓存最大字节数，0 表示禁用缓存。
            cache_max_entries: chunk 解压缓存最大条目数，0 表示禁用缓存。
            retry_limit: 单个 chunk 下载重试次数。

        Raises:
            TypeError: 传入对象不是 PatcherManifest 时抛出。
        """
        if not isinstance(manifest, PatcherManifest):
            raise TypeError("manifest 必须是 PatcherManifest 实例")

        self.manifest = manifest
        self.bundle_url = bundle_url
        self.retry_limit = max(1, retry_limit)
        self._cache = ChunkCache(
            max_bytes=cache_max_bytes,
            max_entries=cache_max_entries,
        )

        logger.debug(
            "WADExtractor 初始化完成: manifest={}, bundle_url={}, "
            "cache_max_entries={}, cache_max_bytes={}",
            getattr(self.manifest, "file", None),
            self.bundle_url,
            self._cache.max_entries,
            self._cache.max_bytes,
        )

    def __enter__(self) -> WADExtractor:
        """进入上下文并返回当前提取器实例."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """退出上下文并释放缓存资源."""
        self.close()

    def close(self):
        """释放提取器持有的缓存资源。."""
        self.clear_cache()

    def clear_cache(self):
        """清空 chunk 解压缓存。."""
        self._cache.clear()

    def cache_stats(self) -> dict[str, int]:
        """返回缓存统计信息。."""
        return self._cache.stats()

    def _cache_get(self, key: tuple[int, int]) -> bytes | None:
        return self._cache.get(key)

    def _cache_put(self, key: tuple[int, int], data: bytes):
        self._cache.put(key, data)

    def _chunk_cache_key(self, chunk: PatcherChunk) -> tuple[int, int]:
        return chunk.bundle.bundle_id, chunk.chunk_id

    def _download_chunk_bytes(self, wad_file: PatcherFile, chunk: PatcherChunk) -> bytes:
        """下载并解压单个 chunk（带有界缓存）。."""
        cache_key = self._chunk_cache_key(chunk)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        if chunk.size <= 0:
            return b""

        bundle_base_url = self.bundle_url or wad_file.manifest.bundle_url
        bundle_url = urljoin(bundle_base_url, f"{chunk.bundle.bundle_id:016X}.bundle")
        headers = {"Range": f"bytes={chunk.offset}-{chunk.offset + chunk.size - 1}"}

        content = b""
        for attempt in range(self.retry_limit):
            try:
                content = http_get_bytes(bundle_url, headers=headers)
                if len(content) != chunk.size:
                    raise DownloadError(
                        f"下载 chunk 失败: chunk_id={chunk.chunk_id:016X}, "
                        f"expected={chunk.size}, actual={len(content)}"
                    )
                break
            except HttpClientError as exc:
                if attempt == self.retry_limit - 1:
                    raise DownloadError(
                        f"下载 chunk 失败: chunk_id={chunk.chunk_id:016X}, retries={self.retry_limit}"
                    ) from exc

        try:
            decompressed = pyzstd.decompress(content)
        except pyzstd.ZstdError as exc:
            raise DecompressError(
                f"解压 chunk 失败: chunk_id={chunk.chunk_id:016X}, bundle_id={chunk.bundle.bundle_id:016X}"
            ) from exc

        if len(decompressed) != chunk.target_size:
            raise DecompressError(
                f"chunk 解压大小不匹配: chunk_id={chunk.chunk_id:016X}, "
                f"expected={chunk.target_size}, actual={len(decompressed)}"
            )

        hash_type = wad_file.chunk_hash_types.get(chunk.chunk_id, 0)
        wad_file.manifest.validate_chunk_hash(
            chunk_data=decompressed,
            chunk_id=chunk.chunk_id,
            hash_type=hash_type,
        )

        self._cache_put(cache_key, decompressed)
        return decompressed

    @staticmethod
    def _collect_chunks_for_range(wad_file: PatcherFile, start: int, length: int) -> tuple[list[PatcherChunk], int]:
        """根据 WAD 内偏移区间挑选 chunks，并返回切片起点。."""
        if start < 0 or length < 0:
            raise ValueError(f"无效区间: start={start}, length={length}")
        if length == 0:
            return [], 0

        end = start + length
        if end > wad_file.size:
            raise ValueError(f"区间超出文件大小: end={end}, file_size={wad_file.size}")

        selected: list[PatcherChunk] = []
        chunk_begin = 0
        first_chunk_start = 0

        for chunk in wad_file.chunks:
            chunk_end = chunk_begin + chunk.target_size

            if chunk_end > start and chunk_begin < end:
                if not selected:
                    first_chunk_start = chunk_begin
                selected.append(chunk)
            if chunk_begin >= end:
                break

            chunk_begin = chunk_end

        if not selected:
            raise ValueError(f"未匹配到区间 chunks: start={start}, length={length}")

        slice_start = start - first_chunk_start
        return selected, slice_start

    def _read_wad_file_range(self, wad_file: PatcherFile, start: int, length: int) -> bytes:
        """按 WAD 文件偏移读取解压后的字节区间。."""
        if length == 0:
            return b""

        selected, slice_start = self._collect_chunks_for_range(wad_file, start=start, length=length)
        raw = b"".join(self._download_chunk_bytes(wad_file, chunk) for chunk in selected)
        slice_end = slice_start + length
        if slice_end > len(raw):
            raise DecompressError(
                f"区间切片越界: start={start}, length={length}, slice_end={slice_end}, raw_len={len(raw)}"
            )
        return raw[slice_start:slice_end]

    def _resolve_path_hash(self, wad_header: WAD, inner_path: str) -> int:
        hash_func = getattr(wad_header, "_get_hash_for_path", None)
        if callable(hash_func):
            return hash_func(inner_path)
        return WAD.get_hash(inner_path)

    def _find_wad_file(self, wad_filename: str) -> PatcherFile | None:
        target = wad_filename.lower()
        for file in self.manifest.files.values():
            if file.name.lower() == target:
                return file

        escaped = re.escape(wad_filename)
        matches = list(self.manifest.filter_files(pattern=escaped))
        if not matches:
            return None
        return matches[0]

    def _build_disk_output_path(self, output_dir: Path, wad_filename: str, inner_path: str) -> Path:
        wad_scope = (output_dir / Path(wad_filename).name).resolve()
        normalized_inner = PurePosixPath(inner_path.replace("\\", "/"))
        if normalized_inner.is_absolute():
            raise ValueError(f"不允许绝对路径: {inner_path}")

        output_path = wad_scope.joinpath(*normalized_inner.parts).resolve()
        if output_path == wad_scope or wad_scope in output_path.parents:
            return output_path
        raise ValueError(f"不允许越界路径: {inner_path}")

    def _extract_files_impl(
        self,
        wad_file_paths: dict[str, list[str]],
        output_dir: Path | None,
    ) -> dict[str, dict[str, bytes | str | None]]:
        results: dict[str, dict[str, bytes | str | None]] = {}
        logger.info("开始提取 WAD 文件内容")

        for wad_filename, target_paths in wad_file_paths.items():
            results[wad_filename] = {}
            logger.debug("处理 WAD: {}, 目标数量={}", wad_filename, len(target_paths))
            wad_file = self._find_wad_file(wad_filename)
            if wad_file is None:
                logger.error("未找到 WAD 文件: {}", wad_filename)
                for target_path in target_paths:
                    results[wad_filename][target_path] = None
                continue

            try:
                wad_header = self.get_wad_header(wad_file)
            except (DownloadError, DecompressError, ValueError) as exc:
                logger.error("读取 WAD 头失败: {}, error={}", wad_filename, exc)
                for target_path in target_paths:
                    results[wad_filename][target_path] = None
                continue

            section_map = {section.path_hash: section for section in wad_header.files}

            for target_path in target_paths:
                path_hash = self._resolve_path_hash(wad_header, target_path)
                section = section_map.get(path_hash)
                if section is None:
                    logger.warning("WAD 内未找到路径: {} -> {}", wad_filename, target_path)
                    results[wad_filename][target_path] = None
                    continue

                try:
                    compressed_data = self._read_wad_file_range(
                        wad_file=wad_file,
                        start=section.offset,
                        length=section.compressed_size,
                    )
                    data = wad_header.extract_by_section(section, "", raw=True, data=compressed_data)
                except (DownloadError, DecompressError, ValueError) as exc:
                    logger.error("提取失败: {} -> {}, error={}", wad_filename, target_path, exc)
                    results[wad_filename][target_path] = None
                    continue

                if output_dir is None:
                    results[wad_filename][target_path] = data
                else:
                    if data is None:
                        results[wad_filename][target_path] = None
                        continue
                    try:
                        output_path = self._build_disk_output_path(output_dir, wad_filename, target_path)
                    except ValueError as exc:
                        logger.error("输出路径非法: {} -> {}, error={}", wad_filename, target_path, exc)
                        results[wad_filename][target_path] = None
                        continue
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_bytes(data)
                    results[wad_filename][target_path] = str(output_path)

        logger.info("完成 WAD 文件提取")
        return results

    def extract_files(self, wad_file_paths: dict[str, list[str]]) -> dict[str, dict[str, bytes | None]]:
        """提取多个 WAD 内部文件并返回 bytes。."""
        raw_results = self._extract_files_impl(wad_file_paths=wad_file_paths, output_dir=None)
        return raw_results  # type: ignore[return-value]

    def extract_files_to_disk(
        self,
        wad_file_paths: dict[str, list[str]],
        output_dir: str,
    ) -> dict[str, dict[str, str | None]]:
        """提取多个 WAD 内部文件并写入磁盘。."""
        disk_results = self._extract_files_impl(wad_file_paths=wad_file_paths, output_dir=Path(output_dir))
        return disk_results  # type: ignore[return-value]

    def get_wad_header(self, wad_file: PatcherFile) -> WAD:
        """读取并解析 WAD 头部信息。."""
        mini_size = min(self.V3_HEADER_MINI_SIZE, wad_file.size)
        wad_header_data = self._read_wad_file_range(wad_file, start=0, length=mini_size)
        header_analyzer = WadHeaderAnalyzer(wad_header_data)

        header_size = min(max(header_analyzer.header_size, mini_size), wad_file.size)
        wad_header_data = self._read_wad_file_range(wad_file, start=0, length=header_size)
        return WAD(wad_header_data)
