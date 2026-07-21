"""sync_many 多清单联合同步单测（mock 网络）."""

import asyncio
import hashlib
import types
from pathlib import Path

from riotmanifest.core.chunk_hash import HASH_TYPE_SHA256
from riotmanifest.downloader import DownloadScheduler, staging_path
from riotmanifest.manifest import PatcherBundle, PatcherFile, PatcherManifest
from riotmanifest.update import ManifestArchive, SyncTarget, sync_many


def _chunk_id(data: bytes) -> int:
    return int.from_bytes(hashlib.sha256(data).digest()[:8], "little")


def _make_manifest(path: Path, *, manifest_id: int = 0, raw: bytes | None = None) -> PatcherManifest:
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
    manifest.raw_bytes = raw
    manifest.manifest_id = manifest_id
    manifest.downloader = DownloadScheduler(manifest)
    return manifest


def _add_file(
    manifest: PatcherManifest,
    name: str,
    chunk_datas: list[bytes],
    *,
    bundle_id: int = 0x1001,
) -> PatcherFile:
    """向 manifest 注册由真实数据块构成的文件."""
    bundle = PatcherBundle(bundle_id)
    chunk_hash_types: dict[int, int] = {}
    for data in chunk_datas:
        chunk_id = _chunk_id(data)
        bundle.add_chunk(chunk_id=chunk_id, size=len(data), target_size=len(data))
        chunk_hash_types[chunk_id] = HASH_TYPE_SHA256
    file = PatcherFile(
        name=name,
        size=sum(len(data) for data in chunk_datas),
        link="",
        flags=None,
        chunks=bundle.chunks,
        manifest=manifest,
        chunk_hash_types=chunk_hash_types,
    )
    manifest.files[name] = file
    return file


def _install_fake_network(manifest: PatcherManifest, chunk_datas: list[bytes], *, fail_bundles: set[int] = frozenset()):
    """安装假网络：按 chunk_id 提供真实数据，记录实际下载的 chunk."""
    data_by_id = {_chunk_id(data): data for data in chunk_datas}
    downloaded_chunk_ids: list[int] = []

    async def fake_run_job(self, session, job, file_pool):
        if job.bundle_id in fail_bundles:
            raise OSError("mock bundle failure")
        for chunk_range in job.ranges:
            for task in chunk_range.tasks:
                data = data_by_id[task.chunk.chunk_id]
                downloaded_chunk_ids.append(task.chunk.chunk_id)
                for target in task.targets:
                    output = staging_path(manifest.file_output(target.file))
                    await asyncio.to_thread(file_pool.write_at, output, data, target.file_offset)
        return 0

    manifest.downloader.run_bundle_job_with_retry = types.MethodType(fake_run_job, manifest.downloader)
    return downloaded_chunk_ids


def _write_local(root: Path, name: str, data: bytes) -> Path:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def test_sync_many_downloads_into_separate_roots(tmp_path: Path):
    d1, d2 = b"aaaa", b"bbbb"
    lcu = _make_manifest(tmp_path / "lcu", manifest_id=0x1, raw=b"raw-lcu")
    _add_file(lcu, "client.dat", [d1], bundle_id=0x1001)
    game = _make_manifest(tmp_path / "game", manifest_id=0x2, raw=b"raw-game")
    _add_file(game, "Game/data.wad", [d2], bundle_id=0x2002)

    _install_fake_network(lcu, [d1])
    _install_fake_network(game, [d2])

    events = []
    results = asyncio.run(
        sync_many(
            [SyncTarget(manifest=lcu), SyncTarget(manifest=game, archive=False)],
            progress_callback=events.append,
        )
    )

    assert len(results) == 2
    assert results[0].failed == [] and results[1].failed == []
    assert (tmp_path / "lcu/client.dat").read_bytes() == d1
    assert (tmp_path / "game/Game/data.wad").read_bytes() == d2
    # 进度为跨清单合计：单一下载批次覆盖两个清单的作业。
    phases = [event.phase for event in events]
    start = events[phases.index("start")]
    assert start.total_jobs == 2
    assert start.total_bytes == 8
    plan_ready = events[phases.index("plan_ready")]
    assert plan_ready.total_jobs == 2
    assert plan_ready.total_bytes == 8
    assert phases.count("start") == 1
    assert phases[-1] == "sync_completed"
    # archive 语义按 target 独立：lcu 推进存档，game 关闭存档。
    assert ManifestArchive(tmp_path / "lcu").load_installed() is not None
    assert not (tmp_path / "game/.rman").exists()


def test_sync_many_partial_failure_isolated_per_target(tmp_path: Path):
    d1, d2 = b"aaaa", b"bbbb"
    lcu = _make_manifest(tmp_path / "lcu", manifest_id=0x1, raw=b"raw-lcu")
    _add_file(lcu, "client.dat", [d1], bundle_id=0x1001)
    game = _make_manifest(tmp_path / "game", manifest_id=0x2, raw=b"raw-game")
    _add_file(game, "Game/data.wad", [d2], bundle_id=0x2002)

    _install_fake_network(lcu, [d1])
    _install_fake_network(game, [d2], fail_bundles={0x2002})

    results = asyncio.run(sync_many([SyncTarget(manifest=lcu), SyncTarget(manifest=game)]))

    assert results[0].failed == []
    assert results[1].failed == ["Game/data.wad"]
    assert len(results[1].failures) == 1
    assert results[1].failures[0].bundle_id == 0x2002
    # 失败只影响所属 target：成功侧推进存档，失败侧不推进。
    assert (tmp_path / "lcu/client.dat").read_bytes() == d1
    assert ManifestArchive(tmp_path / "lcu").load_installed() is not None
    assert ManifestArchive(tmp_path / "game").load_installed() is None


def test_sync_many_empty_targets_returns_empty():
    assert asyncio.run(sync_many([])) == []
