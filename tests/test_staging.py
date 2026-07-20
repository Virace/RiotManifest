"""staging 临时文件写盘与原子替换单测."""

import asyncio
import os
import types
from pathlib import Path

from riotmanifest.core.chunk_hash import HASH_TYPE_SHA256
from riotmanifest.downloader import (
    BundleJob,
    ChunkRange,
    DownloadScheduler,
    FileHandlePool,
)
from riotmanifest.downloader.staging import (
    STAGING_SUFFIX,
    commit_staging,
    discard_staging,
    staging_path,
)
from riotmanifest.manifest import PatcherBundle, PatcherFile, PatcherManifest


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


def _make_file(
    manifest: PatcherManifest,
    *,
    name: str,
    bundle_id: int,
    chunk_id: int,
    chunk_size: int = 4,
) -> PatcherFile:
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


def test_commit_replaces_existing(tmp_path: Path):
    target = tmp_path / "a.bin"
    target.write_bytes(b"old-content")
    Path(staging_path(target)).write_bytes(b"new-content")

    commit_staging(target)

    assert target.read_bytes() == b"new-content"
    assert not os.path.exists(staging_path(target))


def test_discard_keeps_original(tmp_path: Path):
    target = tmp_path / "a.bin"
    target.write_bytes(b"old-content")
    Path(staging_path(target)).write_bytes(b"broken")

    discard_staging(target)
    # 不存在时应静默。
    discard_staging(target)

    assert target.read_bytes() == b"old-content"
    assert not os.path.exists(staging_path(target))


def test_close_path_allows_replace(tmp_path: Path):
    target = tmp_path / "a.bin"
    staging = Path(staging_path(target))
    staging.write_bytes(b"\x00" * 4)

    pool = FileHandlePool(max_handles=4)
    try:
        pool.write_at(staging, b"data", 0)
        pool.close_path(staging)
        # Windows 下句柄未关闭时 os.replace 会抛 PermissionError。
        commit_staging(target)
    finally:
        pool.close()

    assert target.read_bytes() == b"data"


def test_successful_batch_commits_staging(tmp_path: Path):
    """成功批次应把 staging 原子提交为目标文件（即使目标已存在同大小旧内容）."""
    manifest = _make_manifest(tmp_path)
    file = _make_file(manifest, name="a.bin", bundle_id=0x1001, chunk_id=0x2001)
    output = tmp_path / "a.bin"
    # 旧内容与目标大小相同：大小相等不代表内容一致，提交后必须被新内容替换。
    output.write_bytes(b"aaaa")

    async def fake_run_job(self, session, job, file_pool):
        await asyncio.to_thread(
            file_pool.write_at,
            staging_path(manifest.file_output(file)),
            b"newx",
            0,
        )

    manifest.downloader.build_bundle_jobs = types.MethodType(
        lambda self, files: [BundleJob(bundle_id=0x1001, ranges=[ChunkRange(start=0, end=3, tasks=[])])],
        manifest.downloader,
    )
    manifest.downloader.run_bundle_job_with_retry = types.MethodType(
        fake_run_job,
        manifest.downloader,
    )

    results = asyncio.run(manifest.download_files_concurrently([file]))

    assert results == (True,)
    assert output.read_bytes() == b"newx"
    assert not os.path.exists(staging_path(output))


def test_interrupted_batch_leaves_target_intact(tmp_path: Path):
    """失败批次应丢弃 staging，旧文件字节保持原样."""
    manifest = _make_manifest(tmp_path)
    file = _make_file(manifest, name="a.bin", bundle_id=0x1001, chunk_id=0x2001)
    output = tmp_path / "a.bin"
    output.write_bytes(b"aaaa")

    async def fake_run_job(self, session, job, file_pool):
        raise OSError("mock failure")

    manifest.downloader.build_bundle_jobs = types.MethodType(
        lambda self, files: [BundleJob(bundle_id=0x1001, ranges=[ChunkRange(start=0, end=3, tasks=[])])],
        manifest.downloader,
    )
    manifest.downloader.run_bundle_job_with_retry = types.MethodType(
        fake_run_job,
        manifest.downloader,
    )

    results = asyncio.run(manifest.download_files_concurrently([file], raise_on_error=False))

    assert results == (False,)
    assert output.read_bytes() == b"aaaa"
    assert not os.path.exists(staging_path(output))
    assert not list(tmp_path.glob(f"*{STAGING_SUFFIX}"))
