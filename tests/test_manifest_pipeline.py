"""manifest 下载管线核心离线单测。"""

import asyncio
import hashlib
import hmac
import types
from pathlib import Path

import pytest
import pyzstd

from riotmanifest.manifest import (
    BundleJob,
    ChunkRange,
    DecompressError,
    DownloadBatchError,
    DownloadError,
    FileHandlePool,
    GlobalChunkTask,
    HASH_TYPE_HKDF,
    HASH_TYPE_SHA256,
    PatcherBundle,
    PatcherFile,
    PatcherManifest,
    WriteTarget,
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
    return manifest


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

    assert PatcherManifest._compute_chunk_hash(data, HASH_TYPE_SHA256) == sha256_expect
    assert PatcherManifest._compute_chunk_hash(data, HASH_TYPE_HKDF) == hkdf_expect


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

    task_map = manifest._build_global_task_map([file])
    target = task_map[bundle.bundle_id][0].targets[0]
    assert target.chunk_id == chunk.chunk_id
    assert target.hash_type == HASH_TYPE_SHA256


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

    manifest._fetch_ranges_data = types.MethodType(fake_fetch, manifest)
    file_pool = FileHandlePool(max_handles=8)
    try:
        with pytest.raises(DecompressError):
            asyncio.run(manifest._process_bundle_job(None, job, file_pool))
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

    manifest._build_bundle_jobs = types.MethodType(  # type: ignore[method-assign]
        lambda self, files: [BundleJob(bundle_id=0x4004, ranges=[ChunkRange(start=0, end=0, tasks=[])])],
        manifest,
    )
    manifest._run_bundle_job_with_retry = types.MethodType(fake_run_job, manifest)

    with pytest.raises(DownloadBatchError) as exc_info:
        asyncio.run(manifest.download_files_concurrently([file], raise_on_error=True))

    assert len(exc_info.value.failures) == 1
    assert exc_info.value.failures[0].bundle_id == 0x4004
