"""manifest 下载管线核心离线单测."""

import asyncio
import hashlib
import hmac
import types
from pathlib import Path

import pytest
import pyzstd

from riotmanifest.core.chunk_hash import HASH_TYPE_HKDF, HASH_TYPE_SHA256, compute_chunk_hash
from riotmanifest.core.errors import DownloadBatchError
from riotmanifest.downloader import (
    BundleJob,
    ChunkRange,
    DownloadProgress,
    DownloadScheduler,
    FileHandlePool,
    GlobalChunkTask,
    WriteTarget,
)
from riotmanifest.manifest import (
    DecompressError,
    DownloadError,
    PatcherBundle,
    PatcherFile,
    PatcherManifest,
)


def _make_manifest(path: Path) -> PatcherManifest:
    manifest = object.__new__(PatcherManifest)
    manifest.file = "test"
    manifest.path = str(path)
    manifest.bundle_url = "https://example.invalid/"
    manifest.concurrency_limit = 4
    manifest.gap_tolerance = PatcherManifest.DEFAULT_GAP_TOLERANCE
    manifest.max_ranges_per_request = PatcherManifest.DEFAULT_MAX_RANGES_PER_REQUEST
    manifest.max_retries = 1
    manifest.bundles = []
    manifest.chunks = {}
    manifest.flags = {}
    manifest.files = {}
    manifest.downloader = DownloadScheduler(manifest)
    return manifest


def _make_file_with_single_chunk(
    manifest: PatcherManifest,
    *,
    name: str,
    bundle_id: int,
    chunk_id: int,
    chunk_size: int = 1,
) -> PatcherFile:
    """构造仅含单个 chunk 的测试文件."""
    bundle = PatcherBundle(bundle_id)
    bundle.add_chunk(chunk_id=chunk_id, size=chunk_size, target_size=chunk_size)
    return PatcherFile(
        name=name,
        size=chunk_size,
        link="",
        flags=None,
        chunks=bundle.chunks,
        manifest=manifest,
        chunk_hash_types={chunk_id: HASH_TYPE_SHA256},
    )


def _hkdf_reference(chunk_data: bytes) -> int:
    prk = hashlib.sha256(chunk_data).digest()
    buffer = hmac.new(prk, b"\x00\x00\x00\x01", hashlib.sha256).digest()
    result = int.from_bytes(buffer[:8], "little")
    for _ in range(31):
        buffer = hmac.new(prk, buffer, hashlib.sha256).digest()
        result ^= int.from_bytes(buffer[:8], "little")
    return result


def test_compute_chunk_hash_algorithms():
    data = b"manifest-hash-test"
    sha256_expect = int.from_bytes(hashlib.sha256(data).digest()[:8], "little")
    hkdf_expect = _hkdf_reference(data)

    assert compute_chunk_hash(data, HASH_TYPE_SHA256) == sha256_expect
    assert compute_chunk_hash(data, HASH_TYPE_HKDF) == hkdf_expect


def test_build_global_task_map_keeps_hash_type(tmp_path: Path):
    manifest = _make_manifest(tmp_path)
    bundle = PatcherBundle(0x1001)
    bundle.add_chunk(chunk_id=0x2002, size=8, target_size=16)
    chunk = bundle.chunks[0]

    file = PatcherFile(
        name="a.bin",
        size=16,
        link="",
        flags=None,
        chunks=[chunk],
        manifest=manifest,
        chunk_hash_types={chunk.chunk_id: HASH_TYPE_SHA256},
    )

    task_map = manifest.downloader.build_global_task_map([file])
    target = task_map[bundle.bundle_id][0].targets[0]
    assert target.chunk_id == chunk.chunk_id
    assert target.hash_type == HASH_TYPE_SHA256


def test_build_bundle_jobs_schedules_larger_jobs_first(tmp_path: Path):
    manifest = _make_manifest(tmp_path)
    large = _make_file_with_single_chunk(
        manifest,
        name="large.bin",
        bundle_id=0x1010,
        chunk_id=0xA001,
        chunk_size=64,
    )
    small = _make_file_with_single_chunk(
        manifest,
        name="small.bin",
        bundle_id=0x2020,
        chunk_id=0xA002,
        chunk_size=8,
    )
    medium = _make_file_with_single_chunk(
        manifest,
        name="medium.bin",
        bundle_id=0x3030,
        chunk_id=0xA003,
        chunk_size=32,
    )

    jobs = manifest.downloader.build_bundle_jobs([small, medium, large])
    assert [job.bundle_id for job in jobs] == [0x1010, 0x3030, 0x2020]
    assert [manifest.downloader.job_total_bytes(job) for job in jobs] == [64, 32, 8]


def test_dynamic_request_timeout_caps_total_and_sets_sock_read():
    timeout = DownloadScheduler.dynamic_request_timeout(
        total_bytes=10_000_000,
        base_timeout_seconds=10,
        min_transfer_speed_bytes=1,
        max_timeout_seconds=30,
        sock_read_timeout_seconds=12,
    )
    assert timeout.total == 30
    assert timeout.sock_read == 12

    tiny_timeout = DownloadScheduler.dynamic_request_timeout(
        total_bytes=1,
        base_timeout_seconds=10,
        min_transfer_speed_bytes=1024 * 1024,
        max_timeout_seconds=30,
        sock_read_timeout_seconds=20,
    )
    assert tiny_timeout.total == 11
    assert tiny_timeout.sock_read == 11


def test_process_bundle_job_hash_mismatch(tmp_path: Path):
    manifest = _make_manifest(tmp_path)
    raw = b"hash-mismatch"
    compressed = pyzstd.compress(raw)

    expected_chunk_id = int.from_bytes(hashlib.sha256(raw).digest()[:8], "little")
    wrong_chunk_id = expected_chunk_id ^ 0x1

    bundle = PatcherBundle(0x3003)
    bundle.add_chunk(chunk_id=wrong_chunk_id, size=len(compressed), target_size=len(raw))
    chunk = bundle.chunks[0]

    file = PatcherFile(
        name="test.bin",
        size=len(raw),
        link="",
        flags=None,
        chunks=[chunk],
        manifest=manifest,
        chunk_hash_types={chunk.chunk_id: HASH_TYPE_SHA256},
    )

    output = tmp_path / file.name
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as f:
        f.truncate(file.size)

    target = WriteTarget(
        file=file,
        file_offset=0,
        expected_len=len(raw),
        chunk_id=chunk.chunk_id,
        hash_type=HASH_TYPE_SHA256,
    )
    task = GlobalChunkTask(chunk=chunk, targets=[target])
    chunk_range = ChunkRange(start=0, end=len(compressed) - 1, tasks=[task])
    job = BundleJob(bundle_id=bundle.bundle_id, ranges=[chunk_range])

    async def fake_fetch(self, session, bundle_id, ranges):
        return [compressed]

    manifest.downloader.fetch_ranges_data = types.MethodType(fake_fetch, manifest.downloader)
    file_pool = FileHandlePool(max_handles=8)
    try:
        with pytest.raises(DecompressError):
            asyncio.run(manifest.downloader.process_bundle_job(None, job, file_pool))
    finally:
        file_pool.close()


def test_download_files_concurrently_raise_batch_error(tmp_path: Path):
    manifest = _make_manifest(tmp_path)
    bundle = PatcherBundle(0x4004)
    bundle.add_chunk(chunk_id=0x5005, size=1, target_size=1)
    chunk = bundle.chunks[0]
    file = PatcherFile(
        name="error.bin",
        size=1,
        link="",
        flags=None,
        chunks=[chunk],
        manifest=manifest,
        chunk_hash_types={chunk.chunk_id: HASH_TYPE_SHA256},
    )

    async def fake_run_job(self, session, job, file_pool):
        raise DownloadError("mock bundle failure")

    manifest.downloader.build_bundle_jobs = types.MethodType(  # type: ignore[method-assign]
        lambda self, files: [BundleJob(bundle_id=0x4004, ranges=[ChunkRange(start=0, end=0, tasks=[])])],
        manifest.downloader,
    )
    manifest.downloader.run_bundle_job_with_retry = types.MethodType(
        fake_run_job,
        manifest.downloader,
    )

    with pytest.raises(DownloadBatchError) as exc_info:
        asyncio.run(manifest.download_files_concurrently([file], raise_on_error=True))

    assert len(exc_info.value.failures) == 1
    assert exc_info.value.failures[0].bundle_id == 0x4004


def test_download_progress_reports_tick_and_bundle_events(tmp_path: Path):
    manifest = _make_manifest(tmp_path)
    first_file = _make_file_with_single_chunk(
        manifest,
        name="first.bin",
        bundle_id=0x7001,
        chunk_id=0x8101,
    )
    second_file = _make_file_with_single_chunk(
        manifest,
        name="second.bin",
        bundle_id=0x7002,
        chunk_id=0x8102,
    )

    jobs = [
        BundleJob(bundle_id=0x7001, ranges=[ChunkRange(start=0, end=9, tasks=[])]),
        BundleJob(bundle_id=0x7002, ranges=[ChunkRange(start=10, end=29, tasks=[])]),
    ]

    async def fake_run_job(self, session, job, file_pool):  # pylint: disable=unused-argument
        await asyncio.sleep(0.03)

    manifest.downloader.build_bundle_jobs = types.MethodType(
        lambda self, files: jobs,  # pylint: disable=unused-argument
        manifest.downloader,
    )
    manifest.downloader.run_bundle_job_with_retry = types.MethodType(
        fake_run_job,
        manifest.downloader,
    )

    events: list[DownloadProgress] = []
    results = asyncio.run(
        manifest.download_files_concurrently(
            [first_file, second_file],
            concurrency_limit=1,
            progress_callback=events.append,
            progress_interval_seconds=0.01,
        )
    )

    assert results == (True, True)
    assert events[0].phase == "start"
    assert any(event.phase == "tick" for event in events)
    assert sum(1 for event in events if event.phase == "bundle_completed") == 2

    final = events[-1]
    assert final.phase == "completed"
    assert final.total_jobs == 2
    assert final.finished_jobs == 2
    assert final.succeeded_jobs == 2
    assert final.failed_jobs == 0
    assert final.total_bytes == 30
    assert final.finished_bytes == 30
    assert final.progress == 1.0
    assert final.average_speed_bytes_per_sec > 0.0


def test_download_progress_failed_event_when_bundle_errors(tmp_path: Path):
    manifest = _make_manifest(tmp_path)
    first_file = _make_file_with_single_chunk(
        manifest,
        name="ok.bin",
        bundle_id=0x9001,
        chunk_id=0x9101,
    )
    second_file = _make_file_with_single_chunk(
        manifest,
        name="bad.bin",
        bundle_id=0x9002,
        chunk_id=0x9102,
    )

    jobs = [
        BundleJob(bundle_id=0x9001, ranges=[ChunkRange(start=0, end=7, tasks=[])]),
        BundleJob(bundle_id=0x9002, ranges=[ChunkRange(start=8, end=15, tasks=[])]),
    ]

    async def fake_run_job(self, session, job, file_pool):  # pylint: disable=unused-argument
        if job.bundle_id == 0x9002:
            raise DownloadError("mock failed")
        await asyncio.sleep(0.01)

    manifest.downloader.build_bundle_jobs = types.MethodType(
        lambda self, files: jobs,  # pylint: disable=unused-argument
        manifest.downloader,
    )
    manifest.downloader.run_bundle_job_with_retry = types.MethodType(
        fake_run_job,
        manifest.downloader,
    )

    events: list[DownloadProgress] = []
    results = asyncio.run(
        manifest.download_files_concurrently(
            [first_file, second_file],
            concurrency_limit=1,
            raise_on_error=False,
            progress_callback=events.append,
            progress_interval_seconds=0.01,
        )
    )

    assert results == (True, False)
    assert any(event.phase == "bundle_failed" for event in events)
    assert events[-1].phase == "failed"
