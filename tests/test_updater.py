"""ManifestUpdater 增量更新编排器端到端单测（mock 网络）."""

import asyncio
import hashlib
import json
import types
from pathlib import Path

import pytest

from riotmanifest.core.chunk_hash import HASH_TYPE_SHA256
from riotmanifest.downloader import DownloadScheduler, staging_path
from riotmanifest.manifest import PatcherBundle, PatcherFile, PatcherManifest
from riotmanifest.update import FileAction, ManifestArchive, ManifestUpdater, SyncMode
from riotmanifest.update.state import LEGACY_STATE_SCHEMA, STATE_SCHEMA


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


def _save_state(root: Path, manifest: PatcherManifest, files: list[str]) -> ManifestArchive:
    """为 fake manifest 建立与 manifest ID 匹配的 schema 2 安装状态."""
    archive = ManifestArchive(root)
    archive.save(
        manifest.manifest_id,
        manifest.raw_bytes or b"raw-old",
        str(manifest.file),
        files,
    )
    return archive


def test_incremental_sync_downloads_only_missing_chunks(tmp_path: Path):
    d1, d2, d3, d4, d5 = b"aaaa", b"bbbb", b"cccc", b"dddd", b"eeee"

    old = _make_manifest(tmp_path)
    _add_file(old, "keep.bin", [d1])
    _add_file(old, "patch.bin", [d1, d2, d3])
    _add_file(old, "gone.bin", [d2])

    new = _make_manifest(tmp_path, manifest_id=0x2, raw=b"raw-new")
    _add_file(new, "keep.bin", [d1])
    _add_file(new, "patch.bin", [d1, d2, d3, d4, d5])

    _write_local(tmp_path, "keep.bin", d1)
    _write_local(tmp_path, "patch.bin", d1 + d2 + d3)
    _write_local(tmp_path, "gone.bin", d2)
    _save_state(tmp_path, old, ["keep.bin", "patch.bin", "gone.bin"])
    keep_mtime = (tmp_path / "keep.bin").stat().st_mtime_ns

    downloaded = _install_fake_network(new, [d4, d5])

    updater = ManifestUpdater(new, old_manifest=old)
    result = asyncio.run(updater.sync())

    # 只下载新增的 2 个 chunk。
    assert sorted(downloaded) == sorted([_chunk_id(d4), _chunk_id(d5)])
    assert result.downloaded_bytes == 8
    assert result.reused_bytes == 12  # patch.bin 命中 d1+d2+d3
    assert (tmp_path / "patch.bin").read_bytes() == d1 + d2 + d3 + d4 + d5
    # 未变化文件零 IO。
    assert result.actions["keep.bin"] == FileAction.SKIP
    assert (tmp_path / "keep.bin").stat().st_mtime_ns == keep_mtime
    # removed 文件被删除。
    assert result.removed == ["gone.bin"]
    assert not (tmp_path / "gone.bin").exists()
    assert result.failed == []
    assert result.failures == []
    assert result.committed_files == ["patch.bin"]
    state = ManifestArchive(tmp_path).load_installed()
    assert state is not None
    assert state.files == ["keep.bin", "patch.bin"]


def test_moved_file_copied_without_download(tmp_path: Path):
    data = b"aaaabbbb"
    old = _make_manifest(tmp_path)
    _add_file(old, "old/name.bin", [data[:4], data[4:]])
    new = _make_manifest(tmp_path, manifest_id=0x2, raw=b"raw")
    _add_file(new, "new/name.bin", [data[:4], data[4:]])

    _write_local(tmp_path, "old/name.bin", data)
    _save_state(tmp_path, old, ["old/name.bin"])
    downloaded = _install_fake_network(new, [])

    updater = ManifestUpdater(new, old_manifest=old)
    result = asyncio.run(updater.sync())

    assert downloaded == []
    assert result.actions["new/name.bin"] == FileAction.MOVE
    assert (tmp_path / "new/name.bin").read_bytes() == data
    assert result.reused_bytes == 8
    # 移动源在 remove_deleted=True 时清理。
    assert not (tmp_path / "old/name.bin").exists()
    assert result.committed_files == ["new/name.bin"]


def test_verify_only_reports_without_writing(tmp_path: Path):
    d1, d2 = b"aaaa", b"bbbb"
    new = _make_manifest(tmp_path, manifest_id=0x2, raw=b"raw")
    _add_file(new, "a.bin", [d1, d2])
    # 本地第二个 chunk 损坏。
    _write_local(tmp_path, "a.bin", d1 + b"XXXX")

    downloaded = _install_fake_network(new, [d2])

    updater = ManifestUpdater(new)
    result = asyncio.run(updater.sync(mode=SyncMode.VERIFY_ONLY))

    assert result.verify_only
    assert result.missing_bytes == 4
    assert downloaded == []
    # 零写盘：目标内容未变、无 staging、无存档目录。
    assert (tmp_path / "a.bin").read_bytes() == d1 + b"XXXX"
    assert not list(tmp_path.glob("**/*.rman-tmp"))
    assert not (tmp_path / ".rman").exists()
    assert result.committed_files == []


def test_force_full_downloads_everything(tmp_path: Path):
    d1, d2 = b"aaaa", b"bbbb"
    new = _make_manifest(tmp_path, manifest_id=0x2, raw=b"raw")
    _add_file(new, "a.bin", [d1, d2])
    _write_local(tmp_path, "a.bin", d1 + d2)  # 本地已完好也强制重下

    downloaded = _install_fake_network(new, [d1, d2])

    updater = ManifestUpdater(new)
    result = asyncio.run(updater.sync(mode=SyncMode.FORCE_FULL))

    assert sorted(downloaded) == sorted([_chunk_id(d1), _chunk_id(d2)])
    assert result.downloaded_bytes == 8
    assert result.reused_bytes == 0
    assert result.committed_files == ["a.bin"]


def test_no_old_manifest_verifies_locally(tmp_path: Path):
    d1, d2 = b"aaaa", b"bbbb"
    new = _make_manifest(tmp_path, manifest_id=0x2, raw=b"raw")
    _add_file(new, "a.bin", [d1, d2])
    _write_local(tmp_path, "a.bin", d1 + d2)

    downloaded = _install_fake_network(new, [])

    updater = ManifestUpdater(new)
    result = asyncio.run(updater.sync())

    assert downloaded == []
    assert result.downloaded_bytes == 0
    assert result.actions["a.bin"] == FileAction.PATCH
    assert result.reused_bytes == 8
    assert result.committed_files == []


def test_partial_failure_keeps_old_file_and_state(tmp_path: Path):
    d1, d2 = b"aaaa", b"bbbb"
    new = _make_manifest(tmp_path, manifest_id=0x2, raw=b"raw")
    _add_file(new, "ok.bin", [d1], bundle_id=0x1001)
    _add_file(new, "bad.bin", [d2], bundle_id=0x2002)
    _write_local(tmp_path, "bad.bin", b"old!")

    _install_fake_network(new, [d1, d2], fail_bundles={0x2002})

    updater = ManifestUpdater(new)
    result = asyncio.run(updater.sync())

    assert result.failed == ["bad.bin"]
    # 失败详情透传到 UpdateResult，下游可拿到 bundle_id 与原始异常。
    assert len(result.failures) == 1
    assert result.failures[0].bundle_id == 0x2002
    assert "mock bundle failure" in str(result.failures[0].error)
    assert (tmp_path / "ok.bin").read_bytes() == d1
    # 失败文件旧内容保留、无 staging 残留。
    assert (tmp_path / "bad.bin").read_bytes() == b"old!"
    assert not list(tmp_path.glob("**/*.rman-tmp"))
    assert result.committed_files == ["ok.bin"]
    # 部分失败不推进 installed.json。
    assert ManifestArchive(tmp_path).load_installed() is None


def test_repair_mode_fixes_corruption_auto_skips(tmp_path: Path):
    """AUTO 对未变化文件跳过验证（rman --update 权衡）；REPAIR 逐文件验证修复."""
    d1, d2 = b"aaaa", b"bbbb"
    old = _make_manifest(tmp_path)
    _add_file(old, "a.bin", [d1, d2])
    new = _make_manifest(tmp_path, manifest_id=0x2, raw=b"raw")
    _add_file(new, "a.bin", [d1, d2])
    # 本地第二个 chunk 损坏。
    _write_local(tmp_path, "a.bin", d1 + b"XXXX")
    _save_state(tmp_path, old, ["a.bin"])

    downloaded = _install_fake_network(new, [d2])
    updater = ManifestUpdater(new, old_manifest=old)

    result_auto = asyncio.run(updater.sync())
    assert result_auto.actions["a.bin"] == FileAction.SKIP
    assert (tmp_path / "a.bin").read_bytes() == d1 + b"XXXX"
    assert downloaded == []
    assert result_auto.committed_files == []

    result_repair = asyncio.run(updater.sync(mode=SyncMode.REPAIR))
    assert result_repair.actions["a.bin"] == FileAction.PATCH
    assert downloaded == [_chunk_id(d2)]
    assert result_repair.downloaded_bytes == 4
    assert result_repair.reused_bytes == 4
    assert (tmp_path / "a.bin").read_bytes() == d1 + d2
    assert result_repair.committed_files == ["a.bin"]


def test_repair_mode_still_cleans_managed_removed_files(tmp_path: Path):
    data = b"aaaa"
    old = _make_manifest(tmp_path, manifest_id=0x1, raw=b"raw-old")
    _add_file(old, "keep.bin", [data])
    _add_file(old, "gone.bin", [data], bundle_id=0x2002)
    new = _make_manifest(tmp_path, manifest_id=0x2, raw=b"raw-new")
    keep = _add_file(new, "keep.bin", [data])
    _write_local(tmp_path, "keep.bin", data)
    _write_local(tmp_path, "gone.bin", data)
    _save_state(tmp_path, old, ["gone.bin", "keep.bin"])
    _install_fake_network(new, [])

    result = asyncio.run(ManifestUpdater(new, old_manifest=old).sync([keep], mode=SyncMode.REPAIR))

    assert result.actions["keep.bin"] == FileAction.PATCH
    assert result.actions["gone.bin"] == FileAction.REMOVE
    assert result.removed == ["gone.bin"]
    assert not (tmp_path / "gone.bin").exists()


def test_hash_type_migration_same_size_is_patched(tmp_path: Path):
    """哈希类型跨版本迁移 + 大小恰好相同的变更文件必须判 PATCH（Alistar 回归）.

    真实案例：16.3(HKDF) → 16.4(BLAKE3/无 params) 迁移中，
    diff 的 loose 模式会跳过 chunk 比较并把该文件误判 unchanged。
    """
    d1, d2_old, d2_new = b"aaaa", b"bbbb", b"cccc"

    old = _make_manifest(tmp_path)
    _add_file(old, "a.bin", [d1, d2_old])

    # 新清单：内容变化但总大小相同，且 chunk_hash_types 为空（hash_type=0）。
    new = _make_manifest(tmp_path, manifest_id=0x2, raw=b"raw")
    file_new = _add_file(new, "a.bin", [d1, d2_new])
    file_new.chunk_hash_types = {}

    _write_local(tmp_path, "a.bin", d1 + d2_old)
    downloaded = _install_fake_network(new, [d2_new])

    updater = ManifestUpdater(new, old_manifest=old)
    result = asyncio.run(updater.sync())

    assert result.actions["a.bin"] == FileAction.PATCH
    assert downloaded == [_chunk_id(d2_new)]
    # 未变化的 d1 由猜测验证命中复用。
    assert result.reused_bytes == 4
    assert (tmp_path / "a.bin").read_bytes() == d1 + d2_new


def test_successful_sync_archives_manifest(tmp_path: Path):
    d1 = b"aaaa"
    new = _make_manifest(tmp_path, manifest_id=0xABCD, raw=b"raw-manifest")
    _add_file(new, "a.bin", [d1])

    _install_fake_network(new, [d1])

    updater = ManifestUpdater(new)
    result = asyncio.run(updater.sync())

    assert result.failed == []
    archive = ManifestArchive(tmp_path)
    state = archive.load_installed()
    assert state is not None
    assert state.manifest_id == "000000000000ABCD"
    assert state.schema == STATE_SCHEMA
    assert state.files == ["a.bin"]
    assert archive.installed_manifest_path().read_bytes() == b"raw-manifest"


def test_progress_events_cover_full_lifecycle(tmp_path: Path):
    d1, d2 = b"aaaa", b"bbbb"
    new = _make_manifest(tmp_path, manifest_id=0x2, raw=b"raw")
    _add_file(new, "a.bin", [d1, d2])
    _write_local(tmp_path, "a.bin", d1 + b"XXXX")  # 一半命中一半补洞
    _install_fake_network(new, [d2])

    events = []
    updater = ManifestUpdater(new)
    result = asyncio.run(updater.sync(progress_callback=events.append))

    assert result.failed == []
    phases = [event.phase for event in events]
    # 事件序列：verify → plan_ready → 下载阶段 → finalize → 终态。
    assert phases.index("verify") < phases.index("plan_ready") < phases.index("start")
    assert phases.index("completed") < phases.index("finalize")
    assert phases[-1] == "sync_completed"
    # plan_ready 与下载阶段 start 完全同口径（bundle 作业数 / 压缩域字节）。
    plan_ready = events[phases.index("plan_ready")]
    start = events[phases.index("start")]
    assert plan_ready.total_jobs == start.total_jobs
    assert plan_ready.total_bytes == start.total_bytes
    # verify 结束快照：分母为待验证文件解压域合计且已走满。
    verify_events = [event for event in events if event.phase == "verify"]
    assert verify_events[-1].total_bytes == 8
    assert verify_events[-1].finished_bytes == 8


def test_progress_events_when_nothing_to_download(tmp_path: Path):
    d1 = b"aaaa"
    new = _make_manifest(tmp_path, manifest_id=0x2, raw=b"raw")
    _add_file(new, "a.bin", [d1])
    _write_local(tmp_path, "a.bin", d1)
    _install_fake_network(new, [])

    events = []
    updater = ManifestUpdater(new)
    result = asyncio.run(updater.sync(progress_callback=events.append))

    assert result.downloaded_bytes == 0
    phases = [event.phase for event in events]
    # 全命中也能第一时间拿到"没有要下的"：plan_ready 总量为 0。
    plan_ready = events[phases.index("plan_ready")]
    assert plan_ready.total_jobs == 0
    assert plan_ready.total_bytes == 0
    assert "start" not in phases  # 无下载批次
    assert phases[-1] == "sync_completed"


def test_verify_only_emits_plan_ready_with_pending_bytes(tmp_path: Path):
    d1, d2 = b"aaaa", b"bbbb"
    new = _make_manifest(tmp_path, manifest_id=0x2, raw=b"raw")
    _add_file(new, "a.bin", [d1, d2])
    _write_local(tmp_path, "a.bin", d1 + b"XXXX")
    _install_fake_network(new, [d2])

    events = []
    updater = ManifestUpdater(new)
    result = asyncio.run(updater.sync(mode=SyncMode.VERIFY_ONLY, progress_callback=events.append))

    assert result.verify_only
    assert result.missing_bytes == 4
    phases = [event.phase for event in events]
    # dry-run 也报"需要下载多少"（压缩域），但不下载、不 finalize。
    plan_ready = events[phases.index("plan_ready")]
    assert plan_ready.total_jobs == 1
    assert plan_ready.total_bytes == 4
    assert "start" not in phases
    assert "finalize" not in phases
    assert phases[-1] == "sync_completed"


def test_sync_failed_final_phase_on_partial_failure(tmp_path: Path):
    d1, d2 = b"aaaa", b"bbbb"
    new = _make_manifest(tmp_path, manifest_id=0x2, raw=b"raw")
    _add_file(new, "ok.bin", [d1], bundle_id=0x1001)
    _add_file(new, "bad.bin", [d2], bundle_id=0x2002)
    _install_fake_network(new, [d1, d2], fail_bundles={0x2002})

    events = []
    updater = ManifestUpdater(new)
    result = asyncio.run(updater.sync(progress_callback=events.append))

    assert result.failed == ["bad.bin"]
    assert [event.phase for event in events][-1] == "sync_failed"


def test_archive_disabled_skips_rman(tmp_path: Path):
    d1 = b"aaaa"
    new = _make_manifest(tmp_path, manifest_id=0xABCD, raw=b"raw-manifest")
    _add_file(new, "a.bin", [d1])
    _install_fake_network(new, [d1])

    updater = ManifestUpdater(new, archive=False)
    result = asyncio.run(updater.sync())

    assert result.failed == []
    assert (tmp_path / "a.bin").read_bytes() == d1
    # 关闭存档：不创建 .rman、不写 installed.json。
    assert not (tmp_path / ".rman").exists()


def test_repeated_partial_sync_accumulates_same_manifest_coverage(tmp_path: Path):
    d1, d2 = b"aaaa", b"bbbb"
    manifest = _make_manifest(tmp_path, manifest_id=0xABCD, raw=b"raw-manifest")
    first = _add_file(manifest, "Config/description.json", [d1], bundle_id=0x1001)
    second = _add_file(manifest, "DATA/game.wad", [d2], bundle_id=0x2002)
    downloaded = _install_fake_network(manifest, [d1, d2])

    first_result = asyncio.run(ManifestUpdater(manifest).sync([first]))
    first_state = ManifestArchive(tmp_path).load_installed()
    assert first_result.failed == []
    assert first_state is not None
    assert first_state.files == ["Config/description.json"]

    second_result = asyncio.run(ManifestUpdater(manifest, old_manifest=manifest).sync([second]))
    second_state = ManifestArchive(tmp_path).load_installed()

    assert second_result.failed == []
    assert second_result.actions["DATA/game.wad"] == FileAction.PATCH
    assert downloaded == [_chunk_id(d1), _chunk_id(d2)]
    assert second_state is not None
    assert second_state.files == ["Config/description.json", "DATA/game.wad"]


def test_managed_unchanged_missing_from_disk_is_downloaded(tmp_path: Path):
    data = b"aaaa"
    manifest = _make_manifest(tmp_path, manifest_id=0xABCD, raw=b"raw-manifest")
    file = _add_file(manifest, "missing.bin", [data])
    _save_state(tmp_path, manifest, ["missing.bin"])
    downloaded = _install_fake_network(manifest, [data])

    result = asyncio.run(ManifestUpdater(manifest, old_manifest=manifest).sync([file]))

    assert result.actions["missing.bin"] == FileAction.PATCH
    assert downloaded == [_chunk_id(data)]
    assert (tmp_path / "missing.bin").read_bytes() == data


def test_explicit_old_manifest_without_state_cannot_authorize_managed_actions(tmp_path: Path):
    data = b"aaaa"
    old = _make_manifest(tmp_path, manifest_id=0x1)
    _add_file(old, "keep.bin", [data])
    _add_file(old, "gone.bin", [data], bundle_id=0x2002)
    new = _make_manifest(tmp_path, manifest_id=0x2, raw=b"raw-new")
    keep = _add_file(new, "keep.bin", [data])
    _write_local(tmp_path, "keep.bin", data)
    _write_local(tmp_path, "gone.bin", data)
    _install_fake_network(new, [])

    result = asyncio.run(ManifestUpdater(new, old_manifest=old).sync([keep]))

    assert result.actions["keep.bin"] == FileAction.PATCH
    assert "gone.bin" not in result.actions
    assert result.reused_bytes == len(data)
    assert (tmp_path / "gone.bin").is_file()


def test_unmanaged_move_source_is_not_reused(tmp_path: Path):
    data = b"aaaa"
    old = _make_manifest(tmp_path, manifest_id=0x1)
    _add_file(old, "old/name.bin", [data])
    new = _make_manifest(tmp_path, manifest_id=0x2, raw=b"raw-new")
    moved = _add_file(new, "new/name.bin", [data])
    _write_local(tmp_path, "old/name.bin", data)
    downloaded = _install_fake_network(new, [data])

    result = asyncio.run(ManifestUpdater(new, old_manifest=old).sync([moved]))

    assert result.actions["new/name.bin"] == FileAction.PATCH
    assert downloaded == [_chunk_id(data)]
    assert (tmp_path / "new/name.bin").read_bytes() == data
    assert (tmp_path / "old/name.bin").read_bytes() == data


def test_legacy_state_is_diff_hint_then_upgrades_after_verification(tmp_path: Path):
    data = b"aaaa"
    manifest = _make_manifest(tmp_path, manifest_id=0x1, raw=b"raw-manifest")
    file = _add_file(manifest, "a.bin", [data])
    _write_local(tmp_path, "a.bin", data)
    archive = ManifestArchive(tmp_path)
    manifest_path = archive.root / "manifests/0000000000000001.manifest"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_bytes(b"legacy")
    archive.installed_file.write_text(
        json.dumps(
            {
                "schema": LEGACY_STATE_SCHEMA,
                "manifest_id": "0000000000000001",
                "manifest_file": "manifests/0000000000000001.manifest",
                "source": "legacy",
                "updated_at": "2026-01-01T00:00:00Z",
            }
        ),
        "utf-8",
    )
    _install_fake_network(manifest, [])

    result = asyncio.run(ManifestUpdater(manifest, old_manifest=manifest).sync([file]))
    state = archive.load_installed()

    assert result.actions["a.bin"] == FileAction.PATCH
    assert result.reused_bytes == len(data)
    assert state is not None
    assert state.schema == STATE_SCHEMA
    assert state.files == ["a.bin"]


def test_cross_version_carries_only_valid_unchanged_coverage(tmp_path: Path):
    d1, old_data, new_data = b"aaaa", b"bbbb", b"cccc"
    old = _make_manifest(tmp_path, manifest_id=0x1, raw=b"raw-old")
    _add_file(old, "keep.bin", [d1])
    _add_file(old, "change.bin", [old_data], bundle_id=0x2002)
    new = _make_manifest(tmp_path, manifest_id=0x2, raw=b"raw-new")
    _add_file(new, "keep.bin", [d1])
    changed = _add_file(new, "change.bin", [new_data], bundle_id=0x3003)
    _write_local(tmp_path, "keep.bin", d1)
    _write_local(tmp_path, "change.bin", old_data)
    _save_state(tmp_path, old, ["keep.bin", "change.bin"])
    _install_fake_network(new, [new_data])

    result = asyncio.run(ManifestUpdater(new, old_manifest=old).sync([changed]))
    state = ManifestArchive(tmp_path).load_installed()

    assert result.failed == []
    assert state is not None
    assert state.files == ["change.bin", "keep.bin"]


def test_symlink_is_not_carried_as_managed_coverage(tmp_path: Path):
    keep_data, new_data = b"keep", b"new!"
    manifest = _make_manifest(tmp_path, manifest_id=0x2, raw=b"raw-current")
    _add_file(manifest, "keep.bin", [keep_data])
    selected = _add_file(manifest, "new.bin", [new_data], bundle_id=0x2002)
    real = _write_local(tmp_path, "real.bin", keep_data)
    try:
        (tmp_path / "keep.bin").symlink_to(real)
    except OSError as exc:
        pytest.skip(f"当前环境不允许创建符号链接: {exc}")
    _save_state(tmp_path, manifest, ["keep.bin"])
    _install_fake_network(manifest, [new_data])

    result = asyncio.run(ManifestUpdater(manifest).sync([selected]))
    state = ManifestArchive(tmp_path).load_installed()

    assert result.failed == []
    assert state is not None
    assert state.files == ["new.bin"]


def test_matching_managed_state_takes_precedence_over_stale_explicit_hint(tmp_path: Path):
    keep_data, new_data = b"keep", b"new!"
    stale = _make_manifest(tmp_path, manifest_id=0x1)
    manifest = _make_manifest(tmp_path, manifest_id=0x2, raw=b"raw-current")
    _add_file(manifest, "keep.bin", [keep_data])
    selected = _add_file(manifest, "new.bin", [new_data], bundle_id=0x2002)
    _write_local(tmp_path, "keep.bin", keep_data)
    _save_state(tmp_path, manifest, ["keep.bin"])
    _install_fake_network(manifest, [new_data])

    result = asyncio.run(ManifestUpdater(manifest, old_manifest=stale).sync([selected]))
    state = ManifestArchive(tmp_path).load_installed()

    assert result.failed == []
    assert state is not None
    assert state.files == ["keep.bin", "new.bin"]


def test_failed_batch_does_not_cleanup_or_advance_state(tmp_path: Path):
    old_data, ok_data, bad_data = b"old!", b"good", b"bad!"
    old = _make_manifest(tmp_path, manifest_id=0x1, raw=b"raw-old")
    _add_file(old, "gone.bin", [old_data])
    new = _make_manifest(tmp_path, manifest_id=0x2, raw=b"raw-new")
    ok = _add_file(new, "ok.bin", [ok_data], bundle_id=0x1001)
    bad = _add_file(new, "bad.bin", [bad_data], bundle_id=0x2002)
    _write_local(tmp_path, "gone.bin", old_data)
    archive = _save_state(tmp_path, old, ["gone.bin"])
    before = archive.installed_file.read_text("utf-8")
    _install_fake_network(new, [ok_data, bad_data], fail_bundles={0x2002})

    result = asyncio.run(ManifestUpdater(new, old_manifest=old).sync([ok, bad]))

    assert result.failed == ["bad.bin"]
    assert (tmp_path / "ok.bin").read_bytes() == ok_data
    assert (tmp_path / "gone.bin").read_bytes() == old_data
    assert archive.installed_file.read_text("utf-8") == before


def test_archive_false_ignores_and_preserves_existing_state(tmp_path: Path):
    data = b"aaaa"
    old = _make_manifest(tmp_path, manifest_id=0x1, raw=b"raw-old")
    file = _add_file(old, "a.bin", [data])
    _write_local(tmp_path, "a.bin", data)
    archive = _save_state(tmp_path, old, ["a.bin"])
    before = archive.installed_file.read_text("utf-8")
    _install_fake_network(old, [])

    result = asyncio.run(ManifestUpdater(old, old_manifest=old, archive=False).sync([file]))

    assert result.actions["a.bin"] == FileAction.PATCH
    assert result.reused_bytes == len(data)
    assert archive.installed_file.read_text("utf-8") == before


def test_raw_downloader_preserves_existing_installed_state(tmp_path: Path):
    old_data, new_data = b"old!", b"new!"
    old = _make_manifest(tmp_path, manifest_id=0x1, raw=b"raw-old")
    _add_file(old, "managed.bin", [old_data])
    archive = _save_state(tmp_path, old, ["managed.bin"])
    before = archive.installed_file.read_text("utf-8")

    manifest = _make_manifest(tmp_path, manifest_id=0x2, raw=b"raw-new")
    file = _add_file(manifest, "standalone.bin", [new_data])
    _install_fake_network(manifest, [new_data])

    result = asyncio.run(manifest.download_files_concurrently([file]))

    assert result == (True,)
    assert (tmp_path / "standalone.bin").read_bytes() == new_data
    assert archive.installed_file.read_text("utf-8") == before


def test_full_verify_all_complete_commits_nothing(tmp_path: Path):
    """无所有权状态的全量校验完全命中时不产生提交."""
    data_by_name = {
        "a.bin": b"aaaa",
        "b.bin": b"bbbb",
        "c.bin": b"cccc",
    }
    manifest = _make_manifest(tmp_path, manifest_id=0x2, raw=b"raw")
    for name, data in data_by_name.items():
        _add_file(manifest, name, [data])
        _write_local(tmp_path, name, data)
    downloaded = _install_fake_network(manifest, [])

    result = asyncio.run(ManifestUpdater(manifest, archive=False).sync())

    assert result.committed_files == []
    assert downloaded == []
    assert result.downloaded_bytes == 0
    assert all(action is FileAction.PATCH for action in result.actions.values())


def test_committed_files_lists_only_rebuilt_files(tmp_path: Path):
    """只列出实际重建文件，并保持目标文件顺序."""
    complete_a, hit, missing, complete_b, damaged, absent = (
        b"aaaa",
        b"bbbb",
        b"cccc",
        b"dddd",
        b"eeee",
        b"ffff",
    )
    manifest = _make_manifest(tmp_path, manifest_id=0x2, raw=b"raw")
    _add_file(manifest, "complete-a.bin", [complete_a])
    _add_file(manifest, "partial.bin", [hit, missing])
    _add_file(manifest, "complete-b.bin", [complete_b])
    _add_file(manifest, "damaged.bin", [damaged])
    _add_file(manifest, "absent.bin", [absent])
    _write_local(tmp_path, "complete-a.bin", complete_a)
    _write_local(tmp_path, "partial.bin", hit + b"XXXX")
    _write_local(tmp_path, "complete-b.bin", complete_b)
    _write_local(tmp_path, "damaged.bin", b"YYYY")
    _install_fake_network(manifest, [missing, damaged, absent])

    result = asyncio.run(ManifestUpdater(manifest, archive=False).sync())

    assert result.committed_files == ["partial.bin", "damaged.bin", "absent.bin"]
    assert len(result.committed_files) == len(set(result.committed_files))
    assert result.downloaded_bytes == 12
    assert result.reused_bytes == 12


def test_new_file_rebuilt_from_local_hits_is_committed(tmp_path: Path):
    """NEW 文件即使完全由本地 chunk 重建也属于实际提交."""
    data = b"aaaa"
    old = _make_manifest(tmp_path, manifest_id=0x1, raw=b"raw-old")
    manifest = _make_manifest(tmp_path, manifest_id=0x2, raw=b"raw-new")
    file = _add_file(manifest, "a.bin", [data])
    _write_local(tmp_path, "a.bin", data)
    downloaded = _install_fake_network(manifest, [])

    result = asyncio.run(ManifestUpdater(manifest, old_manifest=old, archive=False).sync([file]))

    assert result.actions["a.bin"] is FileAction.NEW
    assert downloaded == []
    assert result.downloaded_bytes == 0
    assert result.reused_bytes == len(data)
    assert result.committed_files == ["a.bin"]


def test_commit_failure_propagates_without_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """提交失败保持原有异常传播语义，不返回不完整结果."""
    data = b"aaaa"
    manifest = _make_manifest(tmp_path, manifest_id=0x2, raw=b"raw")
    _add_file(manifest, "a.bin", [data])
    _install_fake_network(manifest, [data])

    def broken_commit(output):
        raise PermissionError("mock commit failure")

    monkeypatch.setattr("riotmanifest.update.updater.commit_staging", broken_commit)

    with pytest.raises(PermissionError, match="mock commit failure"):
        asyncio.run(ManifestUpdater(manifest).sync())
    assert not (tmp_path / "a.bin").exists()
