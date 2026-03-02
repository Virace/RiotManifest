"""下载传输层对比测试：aiohttp vs urllib3（真实网络集成）."""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import time
import types
from dataclasses import dataclass

import pytest
from urllib3 import PoolManager
from urllib3.util import Timeout

from riotmanifest.core.errors import DownloadError
from riotmanifest.downloader import ChunkRange, DownloadScheduler
from riotmanifest.manifest import PatcherFile, PatcherManifest


@dataclass(frozen=True)
class TransportBenchResult:
    """单次传输层基准结果."""

    transport: str
    elapsed_seconds: float
    downloaded_bytes: int
    throughput_mb_per_sec: float
    file_count: int
    job_count: int
    bundle_count: int
    range_count: int
    unique_chunk_count: int


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    return int(value)


def _parse_urllib3_multipart_response(
    payload: bytes,
    content_type: str,
    ranges: list[ChunkRange],
    bundle_id: int,
    scheduler: DownloadScheduler,
) -> list[bytes]:
    """解析 urllib3 原始响应中的 multipart/byteranges 数据."""
    boundary_match = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type, re.I)
    if boundary_match is None:
        raise DownloadError(f"multipart 缺少 boundary: bundle_id={bundle_id}, content_type={content_type}")

    boundary = (boundary_match.group(1) or boundary_match.group(2) or "").strip()
    if not boundary:
        raise DownloadError(f"multipart boundary 为空: bundle_id={bundle_id}, content_type={content_type}")

    delimiter = b"--" + boundary.encode()
    raw_parts = payload.split(delimiter)
    if len(raw_parts) < 3:
        raise DownloadError(f"multipart 结构异常: bundle_id={bundle_id}, content_type={content_type}")

    index_by_range = {(chunk_range.start, chunk_range.end): idx for idx, chunk_range in enumerate(ranges)}
    mapped_parts: list[bytes | None] = [None] * len(ranges)
    fallback_parts: list[bytes] = []

    for raw_part in raw_parts[1:]:
        if raw_part.startswith(b"--"):
            break

        normalized = raw_part[2:] if raw_part.startswith(b"\r\n") else raw_part
        header_bytes, separator, body_with_tail = normalized.partition(b"\r\n\r\n")
        if not separator:
            continue

        body = body_with_tail[:-2] if body_with_tail.endswith(b"\r\n") else body_with_tail
        headers: dict[str, str] = {}
        for line in header_bytes.split(b"\r\n"):
            key_bytes, sep, value_bytes = line.partition(b":")
            if not sep:
                continue
            key = key_bytes.decode("ascii", errors="ignore").strip().lower()
            value = value_bytes.decode("iso-8859-1", errors="ignore").strip()
            headers[key] = value

        content_range = headers.get("content-range", "")

        mapped = False
        if content_range:
            match = scheduler.CONTENT_RANGE_REGEX.match(content_range)
            if match:
                start = int(match.group(1))
                end = int(match.group(2))
                idx = index_by_range.get((start, end))
                if idx is not None and mapped_parts[idx] is None:
                    mapped_parts[idx] = body
                    mapped = True

        if not mapped:
            fallback_parts.append(body)

    for idx, value in enumerate(mapped_parts):
        if value is None:
            if not fallback_parts:
                raise DownloadError(f"multipart段数不足: bundle_id={bundle_id}, expected={len(ranges)}")
            mapped_parts[idx] = fallback_parts.pop(0)

    if fallback_parts:
        raise DownloadError(f"multipart段数过多: bundle_id={bundle_id}, expected={len(ranges)}, actual>{len(ranges)}")

    return [part for part in mapped_parts if part is not None]


def _fetch_ranges_data_urllib3_sync(
    scheduler: DownloadScheduler,
    pool: PoolManager,
    bundle_id: int,
    ranges: list[ChunkRange],
) -> list[bytes]:
    """使用 urllib3 发起 multi-range 请求并返回分段数据."""
    if not ranges:
        return []

    total_bytes = sum(chunk_range.end - chunk_range.start + 1 for chunk_range in ranges)
    timeout_seconds = scheduler.manifest.DEFAULT_BASE_TIMEOUT_SECONDS + int(
        total_bytes / float(scheduler.manifest.DEFAULT_MIN_TRANSFER_SPEED_BYTES)
    )
    timeout_seconds = min(timeout_seconds, scheduler.manifest.DEFAULT_MAX_TIMEOUT_SECONDS)
    timeout_seconds = max(timeout_seconds, scheduler.manifest.DEFAULT_BASE_TIMEOUT_SECONDS)

    bundle_url = scheduler.manifest.bundle_url.rstrip("/") + f"/{bundle_id:016X}.bundle"
    headers = {
        "Range": scheduler.build_range_header(ranges),
        "Accept-Encoding": "identity",
    }
    timeout = Timeout(connect=30.0, read=float(timeout_seconds))

    try:
        response = pool.request(
            "GET",
            bundle_url,
            headers=headers,
            timeout=timeout,
            retries=False,
            preload_content=True,
        )
    except Exception as exc:  # noqa: BLE001
        raise DownloadError(f"urllib3 请求失败: bundle_id={bundle_id}, error={exc}") from exc

    if response.status not in (200, 206):
        raise DownloadError(f"HTTP状态异常: {response.status}, bundle_id={bundle_id}")

    payload = bytes(response.data or b"")
    content_type = str(response.headers.get("Content-Type", ""))
    content_type_lower = content_type.lower()

    if response.status == 200:
        range_payloads = scheduler.extract_ranges_from_full_body(payload, ranges, bundle_id)
    else:
        if content_type_lower.startswith("multipart/"):
            range_payloads = _parse_urllib3_multipart_response(
                payload=payload,
                content_type=content_type,
                ranges=ranges,
                bundle_id=bundle_id,
                scheduler=scheduler,
            )
        else:
            if len(ranges) != 1:
                raise DownloadError(f"多段range未返回multipart: bundle_id={bundle_id}, ranges={len(ranges)}")
            range_payloads = [payload]

    if len(range_payloads) != len(ranges):
        raise DownloadError(
            f"range响应数量不匹配: bundle_id={bundle_id}, expected={len(ranges)}, actual={len(range_payloads)}"
        )

    for chunk_range, range_payload in zip(ranges, range_payloads, strict=False):
        expected_size = chunk_range.end - chunk_range.start + 1
        if len(range_payload) != expected_size:
            raise DownloadError(
                f"下载range失败: bundle_id={bundle_id}, range={chunk_range.start}-{chunk_range.end}, "
                f"actual={len(range_payload)}, expected={expected_size}"
            )

    return range_payloads


def _resolve_target_files(manifest: PatcherManifest, target_names: list[str]) -> list[PatcherFile]:
    """把文件名列表映射回当前 manifest 的文件对象."""
    files = [manifest.files[name] for name in target_names if name in manifest.files]
    if not files:
        raise RuntimeError("未找到任何可下载目标文件")
    return files


def _collect_metrics(
    *,
    transport: str,
    manifest: PatcherManifest,
    target_files: list[PatcherFile],
    out_dir: str,
    elapsed_seconds: float,
) -> TransportBenchResult:
    """汇总下载结果指标并返回基准结果."""
    downloaded_bytes = 0
    for file in target_files:
        output = os.path.join(out_dir, file.name)
        if not os.path.isfile(output):
            raise RuntimeError(f"{transport} 缺失输出文件: {file.name}")
        size = os.path.getsize(output)
        if size != file.size:
            raise RuntimeError(f"{transport} 文件大小不匹配: {file.name}, actual={size}, expected={file.size}")
        downloaded_bytes += size

    jobs = manifest.downloader.build_bundle_jobs(target_files)
    bundle_count = len({job.bundle_id for job in jobs})
    range_count = sum(len(job.ranges) for job in jobs)
    unique_chunk_count = sum(len(tasks) for tasks in manifest.downloader.build_global_task_map(target_files).values())
    throughput = (downloaded_bytes / (1024 * 1024)) / max(elapsed_seconds, 1e-9)
    return TransportBenchResult(
        transport=transport,
        elapsed_seconds=elapsed_seconds,
        downloaded_bytes=downloaded_bytes,
        throughput_mb_per_sec=throughput,
        file_count=len(target_files),
        job_count=len(jobs),
        bundle_count=bundle_count,
        range_count=range_count,
        unique_chunk_count=unique_chunk_count,
    )


def _run_aiohttp_bench(
    *,
    manifest_url: str,
    target_names: list[str],
    concurrency: int,
) -> TransportBenchResult:
    """运行 aiohttp 传输层基准."""
    with tempfile.TemporaryDirectory(prefix="riot_transport_aiohttp_") as out_dir:
        manifest = PatcherManifest(file=manifest_url, path=out_dir, concurrency_limit=concurrency)
        target_files = _resolve_target_files(manifest, target_names)

        start = time.perf_counter()
        results = asyncio.run(
            manifest.download_files_concurrently(
                target_files,
                concurrency_limit=concurrency,
                raise_on_error=True,
                progress_callback=None,
                progress_interval_seconds=None,
            )
        )
        elapsed_seconds = time.perf_counter() - start
        if not all(results):
            raise RuntimeError("aiohttp 下载存在失败项")

        return _collect_metrics(
            transport="aiohttp",
            manifest=manifest,
            target_files=target_files,
            out_dir=out_dir,
            elapsed_seconds=elapsed_seconds,
        )


def _run_urllib3_bench(
    *,
    manifest_url: str,
    target_names: list[str],
    concurrency: int,
) -> TransportBenchResult:
    """运行 urllib3 传输层基准（保留 multi-range 策略）."""
    with tempfile.TemporaryDirectory(prefix="riot_transport_urllib3_") as out_dir:
        manifest = PatcherManifest(file=manifest_url, path=out_dir, concurrency_limit=concurrency)
        target_files = _resolve_target_files(manifest, target_names)
        scheduler = manifest.downloader
        pool = PoolManager(
            num_pools=max(16, concurrency * 2),
            maxsize=max(32, concurrency * 4),
            retries=False,
        )

        async def _fetch_via_urllib3(self, session, bundle_id, ranges):  # pylint: disable=unused-argument
            return await asyncio.to_thread(
                _fetch_ranges_data_urllib3_sync,
                self,
                pool,
                bundle_id,
                ranges,
            )

        original_fetch = scheduler.fetch_ranges_data
        scheduler.fetch_ranges_data = types.MethodType(_fetch_via_urllib3, scheduler)
        try:
            start = time.perf_counter()
            results = asyncio.run(
                manifest.download_files_concurrently(
                    target_files,
                    concurrency_limit=concurrency,
                    raise_on_error=True,
                    progress_callback=None,
                    progress_interval_seconds=None,
                )
            )
            elapsed_seconds = time.perf_counter() - start
        finally:
            scheduler.fetch_ranges_data = original_fetch
            pool.clear()

        if not all(results):
            raise RuntimeError("urllib3 下载存在失败项")

        return _collect_metrics(
            transport="urllib3",
            manifest=manifest,
            target_files=target_files,
            out_dir=out_dir,
            elapsed_seconds=elapsed_seconds,
        )


@pytest.mark.integration
def test_downloader_transport_compare_on_full_zh_cn_wad():
    """真实网络对比测试：全量下载 zh_CN wad.client，比较传输层表现。."""
    if os.getenv("RIOT_TRANSPORT_BENCH_RUN", "0") != "1":
        pytest.skip("未启用传输层对比基准（设置 RIOT_TRANSPORT_BENCH_RUN=1 可执行）")

    manifest_url = os.getenv(
        "RIOT_TRANSPORT_MANIFEST_URL",
        "https://lol.secure.dyn.riotcdn.net/channels/public/releases/BA80B75282F55531.manifest",
    )
    concurrency = _env_int("RIOT_TRANSPORT_CONCURRENCY", 16)
    mode = os.getenv("RIOT_TRANSPORT_MODE", "both").strip().lower()

    with tempfile.TemporaryDirectory(prefix="riot_transport_pick_") as pick_dir:
        pick_manifest = PatcherManifest(file=manifest_url, path=pick_dir, concurrency_limit=concurrency)
        target_files = [
            file
            for file in pick_manifest.filter_files(flag="zh_CN", pattern="wad.client")
            if not file.link and file.size > 0
        ]
        target_names = [file.name for file in target_files]
        planned_bytes = sum(file.size for file in target_files)

    assert target_names, "未筛选出 zh_CN + wad.client 目标文件"
    print(
        "\n[TRANSPORT] "
        f"manifest={manifest_url}\n"
        f"[TRANSPORT] targets={len(target_names)} planned={planned_bytes / (1024 * 1024):.2f}MB "
        f"concurrency={concurrency}"
    )

    aiohttp_result: TransportBenchResult | None = None
    urllib3_result: TransportBenchResult | None = None

    if mode in ("both", "aiohttp"):
        aiohttp_result = _run_aiohttp_bench(
            manifest_url=manifest_url,
            target_names=target_names,
            concurrency=concurrency,
        )

    if mode in ("both", "urllib3"):
        urllib3_result = _run_urllib3_bench(
            manifest_url=manifest_url,
            target_names=target_names,
            concurrency=concurrency,
        )

    if mode == "aiohttp":
        assert aiohttp_result is not None
        print(
            "[TRANSPORT] "
            f"aiohttp: elapsed={aiohttp_result.elapsed_seconds:.3f}s "
            f"throughput={aiohttp_result.throughput_mb_per_sec:.2f}MB/s "
            f"files={aiohttp_result.file_count} jobs={aiohttp_result.job_count} "
            f"bundles={aiohttp_result.bundle_count} ranges={aiohttp_result.range_count} "
            f"unique_chunks={aiohttp_result.unique_chunk_count}"
        )
        assert aiohttp_result.throughput_mb_per_sec > 0
        return

    if mode == "urllib3":
        assert urllib3_result is not None
        print(
            "[TRANSPORT] "
            f"urllib3: elapsed={urllib3_result.elapsed_seconds:.3f}s "
            f"throughput={urllib3_result.throughput_mb_per_sec:.2f}MB/s "
            f"files={urllib3_result.file_count} jobs={urllib3_result.job_count} "
            f"bundles={urllib3_result.bundle_count} ranges={urllib3_result.range_count} "
            f"unique_chunks={urllib3_result.unique_chunk_count}"
        )
        assert urllib3_result.throughput_mb_per_sec > 0
        return

    assert aiohttp_result is not None
    assert urllib3_result is not None

    speed_ratio = urllib3_result.throughput_mb_per_sec / max(aiohttp_result.throughput_mb_per_sec, 1e-9)
    print(
        "[TRANSPORT] "
        f"aiohttp: elapsed={aiohttp_result.elapsed_seconds:.3f}s "
        f"throughput={aiohttp_result.throughput_mb_per_sec:.2f}MB/s "
        f"files={aiohttp_result.file_count} jobs={aiohttp_result.job_count} "
        f"bundles={aiohttp_result.bundle_count} ranges={aiohttp_result.range_count} "
        f"unique_chunks={aiohttp_result.unique_chunk_count}\n"
        "[TRANSPORT] "
        f"urllib3: elapsed={urllib3_result.elapsed_seconds:.3f}s "
        f"throughput={urllib3_result.throughput_mb_per_sec:.2f}MB/s "
        f"files={urllib3_result.file_count} jobs={urllib3_result.job_count} "
        f"bundles={urllib3_result.bundle_count} ranges={urllib3_result.range_count} "
        f"unique_chunks={urllib3_result.unique_chunk_count}\n"
        "[TRANSPORT] "
        f"speed_ratio(urllib3/aiohttp)={speed_ratio:.3f}"
    )

    assert aiohttp_result.file_count == urllib3_result.file_count
    assert aiohttp_result.downloaded_bytes == urllib3_result.downloaded_bytes
    assert aiohttp_result.throughput_mb_per_sec > 0
    assert urllib3_result.throughput_mb_per_sec > 0
