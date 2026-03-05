"""基于 manifest 按需提取 WAD 内部文件."""

from __future__ import annotations

import re
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
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


@dataclass(frozen=True)
class _PrefetchChunkTask:
    """描述一次 chunk 预取任务."""

    wad_file: PatcherFile
    chunk: PatcherChunk


@dataclass(frozen=True)
class _WADExtractTask:
    """描述一个待提取的 WAD 任务."""

    wad_filename: str
    wad_file: PatcherFile
    wad_header: WAD
    resolved_targets: list[tuple[str, Any]]


class WADExtractor:
    """按需提取 WAD 内部小文件，避免整包下载.

    该提取器适合“少量目标文件”场景，尤其是批量提取多个小文件。
    当单个 WAD 目标文件过多时，整体请求与计算开销会明显升高，
    更建议先下载完整 WAD 再本地提取。
    """

    V3_HEADER_MINI_SIZE = 4 + 268 + 4
    DEFAULT_PREFETCH_CHUNK_CONCURRENCY = 16
    DEFAULT_RECOMMENDED_MAX_TARGETS_PER_WAD = 120

    def __init__(
        self,
        manifest: PatcherManifest,
        bundle_url: str | None = None,
        cache_max_bytes: int = 128 * 1024 * 1024,
        cache_max_entries: int = 512,
        retry_limit: int = RETRY_LIMIT,
        prefetch_chunk_concurrency: int = DEFAULT_PREFETCH_CHUNK_CONCURRENCY,
        recommended_max_targets_per_wad: int = DEFAULT_RECOMMENDED_MAX_TARGETS_PER_WAD,
    ):
        """初始化 WAD 提取器。.

        Args:
            manifest: manifest 实例。
            bundle_url: bundle 基础 URL；为 None 时使用 manifest 内的默认值。
            cache_max_bytes: chunk 解压缓存最大字节数，0 表示禁用缓存。
            cache_max_entries: chunk 解压缓存最大条目数，0 表示禁用缓存。
            retry_limit: 单个 chunk 下载重试次数。
            prefetch_chunk_concurrency: 批量提取时的 chunk 预取并发数，<=1 时禁用预取。
            recommended_max_targets_per_wad: 单个 WAD 建议提取的最大目标文件数。
                超过该值会给出告警，并跳过并发预取。

        Raises:
            TypeError: 传入对象不是 PatcherManifest 时抛出。
        """
        if not isinstance(manifest, PatcherManifest):
            raise TypeError("manifest 必须是 PatcherManifest 实例")

        self.manifest = manifest
        self.bundle_url = bundle_url
        self.retry_limit = max(1, retry_limit)
        self.prefetch_chunk_concurrency = max(1, prefetch_chunk_concurrency)
        self.recommended_max_targets_per_wad = max(1, recommended_max_targets_per_wad)
        self._cache = ChunkCache(
            max_bytes=cache_max_bytes,
            max_entries=cache_max_entries,
        )

        logger.debug(
            "WADExtractor 初始化完成: manifest={}, bundle_url={}, "
            "cache_max_entries={}, cache_max_bytes={}, prefetch_chunk_concurrency={}, "
            "recommended_max_targets_per_wad={}",
            getattr(self.manifest, "file", None),
            self.bundle_url,
            self._cache.max_entries,
            self._cache.max_bytes,
            self.prefetch_chunk_concurrency,
            self.recommended_max_targets_per_wad,
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

    @staticmethod
    def _section_id(section: Any) -> tuple[int, int]:
        """构建 section 的稳定标识，用于去重。."""
        return int(section.offset), int(section.compressed_size)

    def _collect_unique_chunks_for_sections(self, wad_file: PatcherFile, sections: Iterable[Any]) -> list[PatcherChunk]:
        """根据 section 列表计算并去重所需 chunk 集合。."""
        chunk_index: dict[int, PatcherChunk] = {}
        visited_sections: set[tuple[int, int]] = set()
        for section in sections:
            section_id = self._section_id(section)
            if section_id in visited_sections:
                continue
            visited_sections.add(section_id)

            section_chunks, _ = self._collect_chunks_for_range(
                wad_file,
                start=section.offset,
                length=section.compressed_size,
            )
            for chunk in section_chunks:
                chunk_index.setdefault(chunk.chunk_id, chunk)
        return list(chunk_index.values())

    def _prefetch_chunk_tasks(self, chunk_tasks: list[_PrefetchChunkTask]) -> None:
        """并发执行 chunk 预取任务，填充缓存以加速后续提取。."""
        if not chunk_tasks:
            return

        worker_count = min(self.prefetch_chunk_concurrency, len(chunk_tasks))
        if worker_count <= 1:
            for task in chunk_tasks:
                self._download_chunk_bytes(task.wad_file, task.chunk)
            return

        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="wad-prefetch") as executor:
            future_to_task = {
                executor.submit(self._download_chunk_bytes, task.wad_file, task.chunk): task
                for task in chunk_tasks
            }
            for future in as_completed(future_to_task):
                task = future_to_task[future]
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "chunk 预取失败，后续将按需重试: chunk_id={:016X}, bundle_id={:016X}, error={}",
                        task.chunk.chunk_id,
                        task.chunk.bundle.bundle_id,
                        exc,
                    )

    def _prefetch_chunks(self, wad_file: PatcherFile, chunks: list[PatcherChunk]) -> None:
        """并发预取并解压单个 WAD 所需 chunk。."""
        chunk_tasks = [_PrefetchChunkTask(wad_file=wad_file, chunk=chunk) for chunk in chunks]
        self._prefetch_chunk_tasks(chunk_tasks)

    def _prepare_prefetch(self, wad_file: PatcherFile, sections: list[Any]) -> None:
        """在批量小文件提取前预热 chunk 缓存。."""
        if len(sections) <= 1:
            return

        if len(sections) > self.recommended_max_targets_per_wad:
            logger.warning(
                "单个 WAD 目标文件过多({})，当前按需提取方式不建议用于大批量文件；"
                "建议先下载完整 WAD 再本地提取。此次将跳过并发预取。",
                len(sections),
            )
            return

        needed_chunks = self._collect_unique_chunks_for_sections(wad_file, sections)
        logger.debug(
            "开始预取 WAD chunk: wad_file={}, targets={}, unique_chunks={}, concurrency={}",
            wad_file.name,
            len(sections),
            len(needed_chunks),
            self.prefetch_chunk_concurrency,
        )
        self._prefetch_chunks(wad_file, needed_chunks)

    def _collect_global_prefetch_tasks(self, tasks: list[_WADExtractTask]) -> list[_PrefetchChunkTask]:
        """汇总多个 WAD 的预取任务，按 chunk 做全局去重。."""
        task_index: dict[tuple[int, int], _PrefetchChunkTask] = {}

        for task in tasks:
            sections = [section for _, section in task.resolved_targets]
            if len(sections) <= 1:
                continue
            if len(sections) > self.recommended_max_targets_per_wad:
                logger.warning(
                    "单个 WAD 目标文件过多({})，当前按需提取方式不建议用于大批量文件；"
                    "建议先下载完整 WAD 再本地提取。此次将跳过并发预取: {}",
                    len(sections),
                    task.wad_filename,
                )
                continue

            try:
                needed_chunks = self._collect_unique_chunks_for_sections(task.wad_file, sections)
            except ValueError as exc:
                logger.warning(
                    "构建 WAD 预取任务失败，跳过该 WAD 的全局预取: {}, error={}",
                    task.wad_filename,
                    exc,
                )
                continue

            for chunk in needed_chunks:
                cache_key = self._chunk_cache_key(chunk)
                task_index.setdefault(
                    cache_key,
                    _PrefetchChunkTask(
                        wad_file=task.wad_file,
                        chunk=chunk,
                    ),
                )
        return list(task_index.values())

    def _prepare_global_prefetch(self, tasks: list[_WADExtractTask]) -> None:
        """在提取前构建跨 WAD 全局 chunk 池并并发预热缓存。."""
        prefetch_tasks = self._collect_global_prefetch_tasks(tasks)
        if not prefetch_tasks:
            return

        logger.debug(
            "开始全局预取 WAD chunk: wads={}, unique_chunks={}, concurrency={}",
            len(tasks),
            len(prefetch_tasks),
            self.prefetch_chunk_concurrency,
        )
        self._prefetch_chunk_tasks(prefetch_tasks)

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

    def _resolve_wad_extract_tasks(
        self,
        wad_file_paths: dict[str, list[str]],
        results: dict[str, dict[str, bytes | str | None]],
    ) -> list[_WADExtractTask]:
        """解析 WAD 头与目标 section，构建后续提取任务列表。."""
        tasks: list[_WADExtractTask] = []

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
            resolved_targets: list[tuple[str, Any]] = []
            for target_path in target_paths:
                path_hash = self._resolve_path_hash(wad_header, target_path)
                section = section_map.get(path_hash)
                if section is None:
                    logger.warning("WAD 内未找到路径: {} -> {}", wad_filename, target_path)
                    results[wad_filename][target_path] = None
                    continue
                resolved_targets.append((target_path, section))

            tasks.append(
                _WADExtractTask(
                    wad_filename=wad_filename,
                    wad_file=wad_file,
                    wad_header=wad_header,
                    resolved_targets=resolved_targets,
                )
            )
        return tasks

    def _extract_wad_targets(
        self,
        task: _WADExtractTask,
        output_dir: Path | None,
        results: dict[str, dict[str, bytes | str | None]],
    ) -> None:
        """执行单个 WAD 任务并写回提取结果。."""
        for target_path, section in task.resolved_targets:
            try:
                compressed_data = self._read_wad_file_range(
                    wad_file=task.wad_file,
                    start=section.offset,
                    length=section.compressed_size,
                )
                data = task.wad_header.extract_by_section(section, "", raw=True, data=compressed_data)
            except (DownloadError, DecompressError, ValueError) as exc:
                logger.error("提取失败: {} -> {}, error={}", task.wad_filename, target_path, exc)
                results[task.wad_filename][target_path] = None
                continue

            if output_dir is None:
                results[task.wad_filename][target_path] = data
                continue
            if data is None:
                results[task.wad_filename][target_path] = None
                continue

            try:
                output_path = self._build_disk_output_path(output_dir, task.wad_filename, target_path)
            except ValueError as exc:
                logger.error("输出路径非法: {} -> {}, error={}", task.wad_filename, target_path, exc)
                results[task.wad_filename][target_path] = None
                continue

            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(data)
            results[task.wad_filename][target_path] = str(output_path)

    def _extract_files_impl(
        self,
        wad_file_paths: dict[str, list[str]],
        output_dir: Path | None,
    ) -> dict[str, dict[str, bytes | str | None]]:
        results: dict[str, dict[str, bytes | str | None]] = {}
        logger.info("开始提取 WAD 文件内容")

        tasks = self._resolve_wad_extract_tasks(wad_file_paths, results)
        self._prepare_global_prefetch(tasks)

        for task in tasks:
            self._extract_wad_targets(
                task=task,
                output_dir=output_dir,
                results=results,
            )

        logger.info("完成 WAD 文件提取")
        return results

    def extract_files(self, wad_file_paths: dict[str, list[str]]) -> dict[str, dict[str, bytes | None]]:
        """提取多个 WAD 内部文件并返回 bytes.

        注意：
            该接口面向“少量小文件”的按需提取场景。
            若单个 WAD 的目标文件数量过多，建议改为先下载完整 WAD 后本地提取。
        """
        raw_results = self._extract_files_impl(wad_file_paths=wad_file_paths, output_dir=None)
        return raw_results  # type: ignore[return-value]

    def extract_files_to_disk(
        self,
        wad_file_paths: dict[str, list[str]],
        output_dir: str,
    ) -> dict[str, dict[str, str | None]]:
        """提取多个 WAD 内部文件并写入磁盘.

        注意：
            该接口面向“少量小文件”的按需提取场景。
            若单个 WAD 的目标文件数量过多，建议改为先下载完整 WAD 后本地提取。
        """
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
