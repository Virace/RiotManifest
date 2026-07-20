"""调度器按预构建 chunk 条目下载的单测."""

import asyncio
import os
import types
from pathlib import Path

from riotmanifest.core.chunk_hash import HASH_TYPE_SHA256
from riotmanifest.downloader import DownloadScheduler, staging_path
from riotmanifest.manifest import PatcherBundle, PatcherFile, PatcherManifest
from riotmanifest.update.verify import iter_chunk_entries


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


def _make_file_with_chunks(
    manifest: PatcherManifest,
    *,
    name: str,
    bundle_id: int,
    chunk_ids: list[int],
    chunk_size: int = 4,
) -> PatcherFile:
    bundle = PatcherBundle(bundle_id)
    for chunk_id in chunk_ids:
        bundle.add_chunk(chunk_id=chunk_id, size=chunk_size, target_size=chunk_size)
    return PatcherFile(
        name=name,
        size=chunk_size * len(chunk_ids),
        link="",
        flags=None,
        chunks=bundle.chunks,
        manifest=manifest,
        chunk_hash_types={chunk_id: HASH_TYPE_SHA256 for chunk_id in chunk_ids},
    )


def _fake_write_job(manifest: PatcherManifest, payload: bytes):
    """构造把 payload 写入每个 target 对应 staging 偏移的假作业执行器."""

    async def fake_run_job(self, session, job, file_pool):
        for chunk_range in job.ranges:
            for task in chunk_range.tasks:
                for target in task.targets:
                    output = staging_path(manifest.file_output(target.file))
                    await asyncio.to_thread(file_pool.write_at, output, payload, target.file_offset)

    return fake_run_job


def test_task_map_from_entries_covers_subset_only(tmp_path: Path):
    manifest = _make_manifest(tmp_path)
    file = _make_file_with_chunks(
        manifest,
        name="a.bin",
        bundle_id=0x1001,
        chunk_ids=[0x1, 0x2, 0x3],
    )
    entries = list(iter_chunk_entries(file))[1:2]  # 仅第二个 chunk

    task_map = manifest.downloader.build_global_task_map([], entries=entries)

    tasks = task_map[0x1001]
    assert len(tasks) == 1
    assert tasks[0].chunk.chunk_id == 0x2
    assert tasks[0].targets[0].file_offset == 4

    jobs = manifest.downloader.build_bundle_jobs([], entries=entries)
    assert len(jobs) == 1
    # 仅覆盖第二个 chunk 的压缩区间（offset 4..7）。
    assert jobs[0].ranges[0].start == 4
    assert jobs[0].ranges[0].end == 7


def test_download_chunk_entries_without_staging_management(tmp_path: Path):
    manifest = _make_manifest(tmp_path)
    file = _make_file_with_chunks(manifest, name="a.bin", bundle_id=0x1001, chunk_ids=[0x1])
    output = tmp_path / "a.bin"
    # staging 生命周期归调用方：手动预分配。
    with open(staging_path(output), "wb") as f:
        f.truncate(file.size)

    manifest.downloader.run_bundle_job_with_retry = types.MethodType(
        _fake_write_job(manifest, b"newx"),
        manifest.downloader,
    )

    failed = asyncio.run(manifest.downloader.download_chunk_entries(list(iter_chunk_entries(file))))

    assert failed == set()
    # 未托管 staging：不提交，目标文件不存在，staging 保留写入内容。
    assert not output.exists()
    assert Path(staging_path(output)).read_bytes() == b"newx"


def test_download_chunk_entries_manage_staging_commits(tmp_path: Path):
    manifest = _make_manifest(tmp_path)
    file = _make_file_with_chunks(manifest, name="a.bin", bundle_id=0x1001, chunk_ids=[0x1])
    output = tmp_path / "a.bin"

    manifest.downloader.run_bundle_job_with_retry = types.MethodType(
        _fake_write_job(manifest, b"newx"),
        manifest.downloader,
    )

    failed = asyncio.run(
        manifest.downloader.download_chunk_entries(
            list(iter_chunk_entries(file)),
            manage_staging=True,
        )
    )

    assert failed == set()
    assert output.read_bytes() == b"newx"
    assert not os.path.exists(staging_path(output))


def test_download_chunk_entries_reports_failed_paths(tmp_path: Path):
    manifest = _make_manifest(tmp_path)
    ok_file = _make_file_with_chunks(manifest, name="ok.bin", bundle_id=0x1001, chunk_ids=[0x1])
    bad_file = _make_file_with_chunks(manifest, name="bad.bin", bundle_id=0x2002, chunk_ids=[0x2])

    write_job = _fake_write_job(manifest, b"newx")

    async def fake_run_job(self, session, job, file_pool):
        if job.bundle_id == 0x2002:
            raise OSError("mock failure")
        await write_job(self, session, job, file_pool)

    manifest.downloader.run_bundle_job_with_retry = types.MethodType(fake_run_job, manifest.downloader)

    entries = list(iter_chunk_entries(ok_file)) + list(iter_chunk_entries(bad_file))
    failed = asyncio.run(
        manifest.downloader.download_chunk_entries(
            entries,
            manage_staging=True,
            raise_on_error=False,
        )
    )

    assert failed == {"bad.bin"}
    assert (tmp_path / "ok.bin").read_bytes() == b"newx"
    # 失败文件：staging 丢弃、目标不落盘。
    assert not (tmp_path / "bad.bin").exists()
    assert not os.path.exists(staging_path(tmp_path / "bad.bin"))
