"""bundle 作业运行层单测：重试计数、成功传输日志与多组联合调度."""

import asyncio
import types
from pathlib import Path

from loguru import logger

from riotmanifest.core.errors import DownloadError
from riotmanifest.downloader import BundleJob, DownloadScheduler, JobGroup, run_job_groups
from riotmanifest.manifest import PatcherManifest


def _make_manifest(path: Path, *, concurrency: int = 4, max_retries: int = 1) -> PatcherManifest:
    manifest = object.__new__(PatcherManifest)
    manifest.file = "test"
    manifest.path = str(path)
    manifest.bundle_url = "https://example.invalid/"
    manifest.concurrency_limit = concurrency
    manifest.gap_tolerance = PatcherManifest.DEFAULT_GAP_TOLERANCE
    manifest.max_ranges_per_request = PatcherManifest.DEFAULT_MAX_RANGES_PER_REQUEST
    manifest.max_retries = max_retries
    manifest.bundles = []
    manifest.chunks = {}
    manifest.flags = {}
    manifest.files = {}
    manifest.raw_bytes = None
    manifest.manifest_id = 0
    manifest.downloader = DownloadScheduler(manifest)
    return manifest


def test_retry_returns_attempt_count(tmp_path: Path, monkeypatch):
    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr("riotmanifest.downloader.scheduler.asyncio.sleep", _no_sleep)

    manifest = _make_manifest(tmp_path, max_retries=3)
    scheduler = manifest.downloader
    attempts = {"count": 0}

    async def flaky(self, session, job, file_pool):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise DownloadError("mock 前两次失败")

    scheduler.process_bundle_job = types.MethodType(flaky, scheduler)
    job = BundleJob(bundle_id=0x1)

    retries = asyncio.run(scheduler.run_bundle_job_with_retry(session=None, job=job, file_pool=None))

    assert retries == 2
    assert attempts["count"] == 3


def test_success_debug_log_contains_transfer_stats(tmp_path: Path):
    manifest = _make_manifest(tmp_path)
    scheduler = manifest.downloader

    async def fake_ok(self, session, job, file_pool):
        return 1  # 模拟经历一次重试后成功

    scheduler.run_bundle_job_with_retry = types.MethodType(fake_ok, scheduler)
    job = BundleJob(bundle_id=0xABC, total_bytes=1024)

    logger.enable("riotmanifest")
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level="DEBUG")
    try:
        failures = asyncio.run(
            scheduler._run_jobs(
                [job],
                concurrency_limit=1,
                progress_callback=None,
                progress_interval_seconds=None,
            )
        )
    finally:
        logger.remove(sink_id)
        logger.disable("riotmanifest")

    assert failures == []
    done_lines = [line for line in messages if "bundle作业完成" in line]
    assert len(done_lines) == 1
    assert "bytes=1024" in done_lines[0]
    assert "retries=1" in done_lines[0]
    assert "elapsed=" in done_lines[0]
    assert "speed=" in done_lines[0]


def test_run_job_groups_merges_progress_and_attributes_failures(tmp_path: Path):
    manifest_a = _make_manifest(tmp_path / "a")
    manifest_b = _make_manifest(tmp_path / "b")

    async def ok(self, session, job, file_pool):
        return 0

    async def boom(self, session, job, file_pool):
        raise DownloadError("mock b组失败")

    manifest_a.downloader.run_bundle_job_with_retry = types.MethodType(ok, manifest_a.downloader)
    manifest_b.downloader.run_bundle_job_with_retry = types.MethodType(boom, manifest_b.downloader)

    groups = [
        JobGroup(scheduler=manifest_a.downloader, jobs=[BundleJob(bundle_id=0x1, total_bytes=100)]),
        JobGroup(scheduler=manifest_b.downloader, jobs=[BundleJob(bundle_id=0x2, total_bytes=50)]),
    ]
    events = []
    failures = asyncio.run(
        run_job_groups(
            groups,
            progress_callback=events.append,
            progress_interval_seconds=None,
        )
    )

    # 失败按组归属；进度 total 跨组合计。
    assert failures[0] == []
    assert len(failures[1]) == 1
    assert failures[1][0].bundle_id == 0x2
    start = next(event for event in events if event.phase == "start")
    assert start.total_jobs == 2
    assert start.total_bytes == 150
    assert events[-1].phase == "failed"


def test_run_job_groups_empty_returns_empty():
    assert asyncio.run(run_job_groups([])) == []
