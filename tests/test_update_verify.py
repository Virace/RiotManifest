"""chunk 级本地文件固定位置验证单测."""

import hashlib
from pathlib import Path

from riotmanifest.core.chunk_hash import HASH_TYPE_SHA256
from riotmanifest.downloader import DownloadScheduler
from riotmanifest.manifest import PatcherBundle, PatcherFile, PatcherManifest
from riotmanifest.update.verify import iter_chunk_entries, verify_file_chunks


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


def _chunk_id(data: bytes) -> int:
    return int.from_bytes(hashlib.sha256(data).digest()[:8], "little")


def _make_file(manifest: PatcherManifest, name: str, chunk_datas: list[bytes], *, hash_types=None) -> PatcherFile:
    """按数据块列表构造带真实 chunk_id 的测试文件."""
    bundle = PatcherBundle(0x1001)
    chunk_hash_types: dict[int, int] = {}
    for data in chunk_datas:
        chunk_id = _chunk_id(data)
        bundle.add_chunk(chunk_id=chunk_id, size=len(data), target_size=len(data))
        chunk_hash_types[chunk_id] = HASH_TYPE_SHA256
    if hash_types is not None:
        chunk_hash_types = hash_types
    return PatcherFile(
        name=name,
        size=sum(len(data) for data in chunk_datas),
        link="",
        flags=None,
        chunks=bundle.chunks,
        manifest=manifest,
        chunk_hash_types=chunk_hash_types,
    )


def test_iter_chunk_entries_accumulates_offsets(tmp_path: Path):
    manifest = _make_manifest(tmp_path)
    file = _make_file(manifest, "a.bin", [b"aaaa", b"bbbbbb", b"cc"])

    entries = list(iter_chunk_entries(file))

    assert [entry.file_offset for entry in entries] == [0, 4, 10]
    assert [entry.chunk.target_size for entry in entries] == [4, 6, 2]


def test_verify_all_hits(tmp_path: Path):
    manifest = _make_manifest(tmp_path)
    file = _make_file(manifest, "a.bin", [b"aaaa", b"bbbbbb"])
    local = tmp_path / "a.bin"
    local.write_bytes(b"aaaa" + b"bbbbbb")

    result = verify_file_chunks(file, local)

    assert result.exists
    assert result.complete
    assert len(result.hits) == 2
    assert result.misses == []
    assert result.reused_bytes == 10


def test_verify_partial_hit(tmp_path: Path):
    manifest = _make_manifest(tmp_path)
    file = _make_file(manifest, "a.bin", [b"aaaa", b"bbbbbb"])
    local = tmp_path / "a.bin"
    local.write_bytes(b"aaaa" + b"XXXXXX")

    result = verify_file_chunks(file, local)

    assert not result.complete
    assert [entry.file_offset for entry in result.hits] == [0]
    assert [entry.file_offset for entry in result.misses] == [4]


def test_verify_missing_file_all_miss(tmp_path: Path):
    manifest = _make_manifest(tmp_path)
    file = _make_file(manifest, "a.bin", [b"aaaa", b"bb"])

    result = verify_file_chunks(file, tmp_path / "a.bin")

    assert not result.exists
    assert not result.complete
    assert result.hits == []
    assert len(result.misses) == 2


def test_verify_short_file_tail_miss(tmp_path: Path):
    manifest = _make_manifest(tmp_path)
    file = _make_file(manifest, "a.bin", [b"aaaa", b"bbbbbb"])
    local = tmp_path / "a.bin"
    # 本地文件比清单布局短：首块完好，尾块越界即 miss。
    local.write_bytes(b"aaaa" + b"bbb")

    result = verify_file_chunks(file, local)

    assert [entry.file_offset for entry in result.hits] == [0]
    assert [entry.file_offset for entry in result.misses] == [4]


def test_verify_unknown_hash_type_guesses_algorithm(tmp_path: Path):
    manifest = _make_manifest(tmp_path)
    # chunk_hash_types 置空：hash_type 缺省为 0，穷举猜测算法后仍可命中。
    file = _make_file(manifest, "a.bin", [b"aaaa"], hash_types={})
    local = tmp_path / "a.bin"
    local.write_bytes(b"aaaa")

    result = verify_file_chunks(file, local)

    assert len(result.hits) == 1
    assert result.misses == []


def test_verify_unknown_hash_type_mismatch_is_miss(tmp_path: Path):
    manifest = _make_manifest(tmp_path)
    file = _make_file(manifest, "a.bin", [b"aaaa"], hash_types={})
    local = tmp_path / "a.bin"
    # 内容与 chunk_id 无法通过任何算法对上 → miss。
    local.write_bytes(b"XXXX")

    result = verify_file_chunks(file, local)

    assert result.hits == []
    assert len(result.misses) == 1


def test_verify_duplicate_chunk_judged_per_position(tmp_path: Path):
    manifest = _make_manifest(tmp_path)
    file = _make_file(manifest, "a.bin", [b"aaaa", b"aaaa"])
    local = tmp_path / "a.bin"
    # 同一 chunk_id 两个位置：第一处完好、第二处损坏，应按位置独立判定。
    local.write_bytes(b"aaaa" + b"XXXX")

    result = verify_file_chunks(file, local)

    assert [entry.file_offset for entry in result.hits] == [0]
    assert [entry.file_offset for entry in result.misses] == [4]
