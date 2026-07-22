"""多 bundle URL 分摊与重试换域名单测."""

import asyncio
from pathlib import Path

import pytest

from riotmanifest.core.errors import DownloadError
from riotmanifest.downloader import DownloadScheduler
from riotmanifest.downloader.file_pool import FileHandlePool
from riotmanifest.manifest import PatcherBundle, PatcherFile, PatcherManifest

URL_A = "https://mirror-a.invalid/bundles/"
URL_B = "https://mirror-b.invalid/bundles/"


def _make_manifest(path: Path, *, bundle_urls: list[str] | None = None, max_retries: int = 1) -> PatcherManifest:
    manifest = object.__new__(PatcherManifest)
    manifest.file = "test"
    manifest.path = str(path)
    manifest.bundle_urls = bundle_urls or [URL_A]
    manifest.bundle_url = manifest.bundle_urls[0]
    manifest.concurrency_limit = 4
    manifest.gap_tolerance = 0
    manifest.max_ranges_per_request = PatcherManifest.DEFAULT_MAX_RANGES_PER_REQUEST
    manifest.full_bundle_threshold = None
    manifest.max_retries = max_retries
    manifest.bundles = []
    manifest.chunks = {}
    manifest.flags = {}
    manifest.files = {}
    manifest.downloader = DownloadScheduler(manifest)
    return manifest


def _make_single_chunk_file(manifest: PatcherManifest, *, bundle_id: int, size: int = 8) -> PatcherFile:
    bundle = PatcherBundle(bundle_id)
    bundle.add_chunk(chunk_id=bundle_id * 0x10, size=size, target_size=size)
    return PatcherFile(
        name=f"f{bundle_id:x}.bin",
        size=size,
        link="",
        flags=None,
        chunks=bundle.chunks,
        manifest=manifest,
        chunk_hash_types={},
    )


class _FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body
        self.headers = {}

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """按调用序返回预设响应并记录请求 URL 的假会话."""

    def __init__(self, responses: list[_FakeResponse]):
        self._responses = responses
        self.urls: list[str] = []

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        return self._responses[min(len(self.urls) - 1, len(self._responses) - 1)]


def test_ctor_bundle_urls_plumbing(tmp_path: Path):
    manifest = object.__new__(PatcherManifest)
    with pytest.raises(ValueError, match="file can't be empty"):
        manifest.__init__(None, str(tmp_path), bundle_urls=[URL_A, URL_B])
    assert manifest.bundle_urls == [URL_A, URL_B]
    assert manifest.bundle_url == URL_A

    single = object.__new__(PatcherManifest)
    with pytest.raises(ValueError, match="file can't be empty"):
        single.__init__(None, str(tmp_path), bundle_url="https://only.invalid/b/")
    assert single.bundle_urls == ["https://only.invalid/b/"]
    assert single.bundle_url == "https://only.invalid/b/"


def test_jobs_spread_deterministically_across_urls(tmp_path: Path):
    manifest = _make_manifest(tmp_path, bundle_urls=[URL_A, URL_B])
    even_job = manifest.downloader.build_bundle_jobs([_make_single_chunk_file(manifest, bundle_id=0x2)])[0]
    odd_job = manifest.downloader.build_bundle_jobs([_make_single_chunk_file(manifest, bundle_id=0x3)])[0]

    session = _FakeSession([_FakeResponse(206, b"x" * 8)])
    asyncio.run(manifest.downloader.fetch_ranges_data(session, even_job.bundle_id, even_job.ranges))
    asyncio.run(manifest.downloader.fetch_ranges_data(session, odd_job.bundle_id, odd_job.ranges))

    assert session.urls[0].startswith(URL_A)
    assert session.urls[1].startswith(URL_B)


def test_retry_switches_to_next_url(tmp_path: Path, monkeypatch):
    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr("riotmanifest.downloader.scheduler.asyncio.sleep", _no_sleep)

    manifest = _make_manifest(tmp_path, bundle_urls=[URL_A, URL_B], max_retries=2)
    file = _make_single_chunk_file(manifest, bundle_id=0x2)
    manifest.preallocate_file(file)
    job = manifest.downloader.build_bundle_jobs([file])[0]

    # 两次尝试均返回 500：只验证重试是否切换基础 URL，不关心载荷成功路径。
    session = _FakeSession([_FakeResponse(500, b""), _FakeResponse(500, b"")])
    file_pool = FileHandlePool(max_handles=4)
    try:
        with pytest.raises(DownloadError):
            asyncio.run(
                manifest.downloader.run_bundle_job_with_retry(session=session, job=job, file_pool=file_pool)
            )
    finally:
        file_pool.close()

    assert len(session.urls) == 2
    assert session.urls[0].startswith(URL_A)
    assert session.urls[1].startswith(URL_B)


def test_legacy_manifest_stub_without_bundle_urls_still_works(tmp_path: Path):
    """兼容旧式 stub / 序列化对象：无 bundle_urls 属性时回退单 bundle_url."""
    manifest = _make_manifest(tmp_path)
    del manifest.bundle_urls
    file = _make_single_chunk_file(manifest, bundle_id=0x7)
    job = manifest.downloader.build_bundle_jobs([file])[0]

    session = _FakeSession([_FakeResponse(206, b"x" * 8)])
    payloads = asyncio.run(manifest.downloader.fetch_ranges_data(session, job.bundle_id, job.ranges))

    assert payloads == [b"x" * 8]
    assert session.urls[0].startswith(URL_A)
