"""整包 GET（full_bundle）判定与请求路径单测."""

import asyncio
from pathlib import Path

import pytest

from riotmanifest.core.errors import DownloadError
from riotmanifest.downloader import DownloadScheduler
from riotmanifest.manifest import PatcherBundle, PatcherFile, PatcherManifest


def _make_manifest(path: Path, *, gap_tolerance: int = 0, threshold: float | None = 0.7) -> PatcherManifest:
    manifest = object.__new__(PatcherManifest)
    manifest.file = "test"
    manifest.path = str(path)
    manifest.bundle_url = "https://example.invalid/bundles/"
    manifest.concurrency_limit = 4
    manifest.gap_tolerance = gap_tolerance
    manifest.max_ranges_per_request = PatcherManifest.DEFAULT_MAX_RANGES_PER_REQUEST
    manifest.full_bundle_threshold = threshold
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
    bundle_id: int,
    chunk_count: int,
    chunk_size: int = 100,
    pick: list[int] | None = None,
    name: str = "a.bin",
) -> PatcherFile:
    """构造单 bundle 文件；pick 给定时文件只引用被选中下标的 chunk（稀疏覆盖）."""
    bundle = PatcherBundle(bundle_id)
    for index in range(chunk_count):
        bundle.add_chunk(chunk_id=bundle_id * 0x1000 + index, size=chunk_size, target_size=chunk_size)
    chunks = bundle.chunks if pick is None else [bundle.chunks[i] for i in pick]
    return PatcherFile(
        name=name,
        size=chunk_size * len(chunks),
        link="",
        flags=None,
        chunks=chunks,
        manifest=manifest,
        chunk_hash_types={},
    )


class _FakeResponse:
    def __init__(self, status: int, body: bytes, headers: dict | None = None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, response: _FakeResponse):
        self._response = response
        self.calls: list[dict] = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers or {})})
        return self._response


def test_ctor_defaults_and_overrides(tmp_path: Path):
    # 属性赋值先于 file 校验，借 ValueError 路径检验参数装配。
    manifest = object.__new__(PatcherManifest)
    with pytest.raises(ValueError, match="file can't be empty"):
        manifest.__init__(None, str(tmp_path))
    assert manifest.gap_tolerance == 32 * 1024
    assert manifest.max_ranges_per_request == 30
    assert manifest.full_bundle_threshold is None
    assert manifest.concurrency_limit == 16

    overridden = object.__new__(PatcherManifest)
    with pytest.raises(ValueError, match="file can't be empty"):
        overridden.__init__(
            None,
            str(tmp_path),
            concurrency_limit=8,
            gap_tolerance=1024 * 1024,
            max_ranges_per_request=10,
            full_bundle_threshold=PatcherManifest.SUGGESTED_FULL_BUNDLE_THRESHOLD,
        )
    assert overridden.gap_tolerance == 1024 * 1024
    assert overridden.max_ranges_per_request == 10
    assert overridden.full_bundle_threshold == 0.7
    assert overridden.concurrency_limit == 8


def test_full_coverage_produces_single_full_bundle_job(tmp_path: Path):
    manifest = _make_manifest(tmp_path)
    file = _make_file(manifest, bundle_id=0x1, chunk_count=10)

    jobs = manifest.downloader.build_bundle_jobs([file])

    assert len(jobs) == 1
    assert jobs[0].full_bundle is True
    assert jobs[0].total_bytes == 1000
    # ranges 保留用于本地切片。
    assert jobs[0].ranges


def test_sparse_coverage_keeps_range_jobs(tmp_path: Path):
    manifest = _make_manifest(tmp_path)
    file = _make_file(manifest, bundle_id=0x1, chunk_count=10, pick=[0, 5])

    jobs = manifest.downloader.build_bundle_jobs([file])

    assert len(jobs) == 1
    assert jobs[0].full_bundle is False
    assert jobs[0].total_bytes == 200
    assert len(jobs[0].ranges) == 2


def test_threshold_none_disables_full_bundle(tmp_path: Path):
    manifest = _make_manifest(tmp_path, threshold=None)
    file = _make_file(manifest, bundle_id=0x1, chunk_count=10)

    jobs = manifest.downloader.build_bundle_jobs([file])

    assert len(jobs) == 1
    assert jobs[0].full_bundle is False
    assert jobs[0].total_bytes == 1000


def test_full_bundle_job_not_split_by_max_ranges(tmp_path: Path):
    # 隔一取一：50 个 range、覆盖率 0.5。
    manifest = _make_manifest(tmp_path, threshold=0.4)
    file = _make_file(manifest, bundle_id=0x1, chunk_count=100, chunk_size=10, pick=list(range(0, 100, 2)))

    jobs = manifest.downloader.build_bundle_jobs([file])

    assert len(jobs) == 1
    assert jobs[0].full_bundle is True
    assert len(jobs[0].ranges) == 50
    assert jobs[0].total_bytes == 1000


def test_below_threshold_many_ranges_split_as_before(tmp_path: Path):
    # 同样 50 个 range，但阈值 0.6 > 覆盖率 0.5：维持 range 作业并按 30 段拆分。
    manifest = _make_manifest(tmp_path, threshold=0.6)
    file = _make_file(manifest, bundle_id=0x1, chunk_count=100, chunk_size=10, pick=list(range(0, 100, 2)))

    jobs = manifest.downloader.build_bundle_jobs([file])

    assert len(jobs) == 2
    assert all(job.full_bundle is False for job in jobs)
    assert sum(len(job.ranges) for job in jobs) == 50


def test_fetch_full_bundle_sends_no_range_header_and_slices(tmp_path: Path):
    manifest = _make_manifest(tmp_path)
    file = _make_file(manifest, bundle_id=0x1, chunk_count=10)
    jobs = manifest.downloader.build_bundle_jobs([file])
    job = jobs[0]

    body = bytes(range(256)) * 4  # 1024 字节 > bundle_size=1000
    session = _FakeSession(_FakeResponse(200, body[:1000]))

    payloads = asyncio.run(
        manifest.downloader.fetch_ranges_data(
            session,
            job.bundle_id,
            job.ranges,
            full_bundle=True,
            expected_bytes=job.total_bytes,
        )
    )

    assert "Range" not in session.calls[0]["headers"]
    assert session.calls[0]["url"].endswith("0000000000000001.bundle")
    for chunk_range, payload in zip(job.ranges, payloads, strict=True):
        assert payload == body[:1000][chunk_range.start : chunk_range.end + 1]


def test_fetch_range_mode_still_sends_range_header(tmp_path: Path):
    manifest = _make_manifest(tmp_path)
    file = _make_file(manifest, bundle_id=0x1, chunk_count=10, pick=[0])
    jobs = manifest.downloader.build_bundle_jobs([file])

    job = jobs[0]
    assert job.full_bundle is False
    session = _FakeSession(
        _FakeResponse(
            206,
            b"x" * 100,
            {"Content-Range": "bytes 0-99/1000"},
        )
    )

    payloads = asyncio.run(manifest.downloader.fetch_ranges_data(session, job.bundle_id, job.ranges))

    assert session.calls[0]["headers"]["Range"] == "bytes=0-99"
    assert payloads == [b"x" * 100]


def test_fetch_full_bundle_truncated_body_raises(tmp_path: Path):
    manifest = _make_manifest(tmp_path)
    file = _make_file(manifest, bundle_id=0x1, chunk_count=10)
    job = manifest.downloader.build_bundle_jobs([file])[0]

    session = _FakeSession(_FakeResponse(200, b"x" * 500))

    with pytest.raises(DownloadError):
        asyncio.run(
            manifest.downloader.fetch_ranges_data(
                session,
                job.bundle_id,
                job.ranges,
                full_bundle=True,
                expected_bytes=job.total_bytes,
            )
        )
