import asyncio
import io
import struct
import types
from pathlib import Path

import pytest
import pyzstd

from riotmanifest.http_client import HttpClientError
from riotmanifest.manifest import (
    BinaryParser,
    BundleJob,
    ChunkRange,
    DecompressError,
    DownloadError,
    FileHandlePool,
    PatcherBundle,
    PatcherFile,
    PatcherManifest,
)


def _make_manifest_stub(path: Path | None = None) -> PatcherManifest:
    manifest = object.__new__(PatcherManifest)
    manifest.file = "stub.manifest"
    manifest.path = str(path) if path is not None else ""
    manifest.bundle_url = "https://example.invalid/bundles/"
    manifest.concurrency_limit = 4
    manifest.gap_tolerance = PatcherManifest.DEFAULT_GAP_TOLERANCE
    manifest.max_ranges_per_request = PatcherManifest.DEFAULT_MAX_RANGES_PER_REQUEST
    manifest.max_retries = 2
    manifest.bundles = []
    manifest.chunks = {}
    manifest.flags = {}
    manifest.files = {}
    return manifest


def _make_file_with_chunk(
    manifest: PatcherManifest,
    *,
    name: str,
    chunk_id: int,
    compressed_size: int,
    target_size: int,
    chunk_hash_types: dict[int, int] | None = None,
) -> tuple[PatcherFile, object]:
    bundle = PatcherBundle(0x1001)
    bundle.add_chunk(chunk_id=chunk_id, size=compressed_size, target_size=target_size)
    chunk = bundle.chunks[0]
    file = PatcherFile(
        name=name,
        size=target_size,
        link="",
        flags=None,
        chunks=[chunk],
        manifest=manifest,
        chunk_hash_types=chunk_hash_types or {},
    )
    return file, chunk


def test_file_handle_pool_evicts_unreferenced_entry(tmp_path: Path):
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"\x00\x00")
    second.write_bytes(b"\x00\x00")

    pool = FileHandlePool(max_handles=1)
    pool.write_at(first, b"A", 0)
    first_entry = pool._handles[str(first)]

    pool.write_at(second, b"B", 0)

    assert first_entry.evicted is True
    assert first_entry.file_obj.closed
    assert first.read_bytes() == b"A\x00"
    assert second.read_bytes() == b"B\x00"
    pool.close()


def test_file_handle_pool_release_closes_evicted_referenced_entry(tmp_path: Path):
    target = tmp_path / "target.bin"
    target.write_bytes(b"\x00")

    pool = FileHandlePool(max_handles=1)
    entry = pool._acquire(target)
    with pool._lock:
        to_close = pool._evict_one_locked()

    assert to_close == []
    assert entry.evicted is True

    pool._release(entry)
    assert entry.file_obj.closed
    pool.close()


def test_patcher_file_download_chunk_retries_then_uses_cache(monkeypatch, tmp_path: Path):
    manifest = _make_manifest_stub(tmp_path)
    raw = b"DATA"
    compressed = pyzstd.compress(raw)
    patcher_file, chunk = _make_file_with_chunk(
        manifest,
        name="retry.bin",
        chunk_id=0x2001,
        compressed_size=len(compressed),
        target_size=len(raw),
    )

    calls = {"http": 0, "validate": 0}

    def _fake_http_get_bytes(url: str, headers=None):
        calls["http"] += 1
        if calls["http"] == 1:
            raise HttpClientError("temporary")
        assert headers == {"Range": f"bytes={chunk.offset}-{chunk.offset + chunk.size - 1}"}
        assert url.endswith(f"{chunk.bundle.bundle_id:016X}.bundle")
        return compressed

    def _fake_validate(self, chunk_data: bytes, chunk_id: int, hash_type: int):
        calls["validate"] += 1
        assert chunk_data == raw
        assert chunk_id == chunk.chunk_id
        assert hash_type == 0

    monkeypatch.setattr("riotmanifest.manifest.http_get_bytes", _fake_http_get_bytes)
    manifest._validate_chunk_hash = types.MethodType(_fake_validate, manifest)

    assert patcher_file.download_chunk(chunk) == raw
    assert patcher_file.download_chunk(chunk) == raw
    assert calls["http"] == 2
    assert calls["validate"] == 1


def test_patcher_file_download_chunk_size_mismatch_raises(monkeypatch, tmp_path: Path):
    manifest = _make_manifest_stub(tmp_path)
    patcher_file, chunk = _make_file_with_chunk(
        manifest,
        name="mismatch.bin",
        chunk_id=0x2002,
        compressed_size=5,
        target_size=5,
    )

    monkeypatch.setattr("riotmanifest.manifest.http_get_bytes", lambda url, headers=None: b"1234")

    with pytest.raises(DownloadError, match="获取到"):
        patcher_file.download_chunk(chunk)


def test_patcher_file_download_chunk_decompress_error(monkeypatch, tmp_path: Path):
    manifest = _make_manifest_stub(tmp_path)
    patcher_file, chunk = _make_file_with_chunk(
        manifest,
        name="decompress.bin",
        chunk_id=0x2003,
        compressed_size=4,
        target_size=4,
    )

    monkeypatch.setattr("riotmanifest.manifest.http_get_bytes", lambda url, headers=None: b"ABCD")

    with pytest.raises(DecompressError, match="解压缩chunk"):
        patcher_file.download_chunk(chunk)


def test_patcher_file_download_chunks_combines_data(monkeypatch, tmp_path: Path):
    manifest = _make_manifest_stub(tmp_path)
    bundle = PatcherBundle(0x1001)
    bundle.add_chunk(chunk_id=0x9001, size=1, target_size=1)
    bundle.add_chunk(chunk_id=0x9002, size=1, target_size=1)

    patcher_file = PatcherFile(
        name="combine.bin",
        size=2,
        link="",
        flags=None,
        chunks=bundle.chunks,
        manifest=manifest,
        chunk_hash_types={},
    )

    payloads = {0x9001: b"A", 0x9002: b"B"}
    monkeypatch.setattr(PatcherFile, "download_chunk", lambda self, chunk: payloads[chunk.chunk_id])

    assert patcher_file.download_chunks(patcher_file.chunks) == b"AB"


def test_manifest_init_rejects_empty_file(tmp_path: Path):
    with pytest.raises(ValueError, match="file can't be empty"):
        PatcherManifest(file="", path=str(tmp_path))


def test_manifest_init_from_local_file(monkeypatch, tmp_path: Path):
    source = tmp_path / "sample.manifest"
    source.write_bytes(b"LOCAL")
    captured = {}

    def _fake_parse_rman(self, file_obj):
        captured["payload"] = file_obj.read()

    monkeypatch.setattr(PatcherManifest, "parse_rman", _fake_parse_rman)
    manifest = PatcherManifest(file=str(source), path=str(tmp_path), max_retries=0)

    assert captured["payload"] == b"LOCAL"
    assert manifest.max_retries == 1


def test_manifest_init_accepts_pathlike(monkeypatch, tmp_path: Path):
    source = tmp_path / "pathlike.manifest"
    source.write_bytes(b"PATHLIKE")
    captured = {}

    def _fake_parse_rman(self, file_obj):
        captured["payload"] = file_obj.read()

    monkeypatch.setattr(PatcherManifest, "parse_rman", _fake_parse_rman)
    manifest = PatcherManifest(file=source, path=tmp_path)

    assert captured["payload"] == b"PATHLIKE"
    assert manifest.file == str(source)


def test_manifest_init_from_url(monkeypatch, tmp_path: Path):
    captured = {}

    def _fake_parse_rman(self, file_obj):
        captured["payload"] = file_obj.read()

    monkeypatch.setattr("riotmanifest.manifest.http_get_bytes", lambda url: b"REMOTE")
    monkeypatch.setattr(PatcherManifest, "parse_rman", _fake_parse_rman)

    PatcherManifest(file="https://example.invalid/test.manifest", path=str(tmp_path))
    assert captured["payload"] == b"REMOTE"


def test_manifest_init_rejects_invalid_path(tmp_path: Path):
    missing = tmp_path / "not-exists.manifest"
    with pytest.raises(ValueError, match="file error"):
        PatcherManifest(file=str(missing), path=str(tmp_path))


def test_filter_files_with_pattern_and_flags(tmp_path: Path):
    manifest = _make_manifest_stub(tmp_path)

    file_a = PatcherFile("DATA/a.bin", 1, "", ["zh_CN"], [], manifest)
    file_b = PatcherFile("DATA/b.wad.client", 1, "", ["en_US"], [], manifest)
    file_c = PatcherFile("Assets/c.bin", 1, "", None, [], manifest)
    manifest.files = {item.name: item for item in [file_a, file_b, file_c]}

    assert list(manifest.filter_files()) == [file_a, file_b, file_c]
    assert list(manifest.filter_files(pattern=r"\.bin$")) == [file_a, file_c]
    assert list(manifest.filter_files(flag="zh_CN")) == [file_a]
    assert list(manifest.filter_files(pattern="data", flag=["en_US"])) == [file_b]


def test_download_files_concurrently_handles_non_raising_failures(tmp_path: Path):
    manifest = _make_manifest_stub(tmp_path)
    data_file, chunk = _make_file_with_chunk(
        manifest,
        name="data.bin",
        chunk_id=0x3001,
        compressed_size=1,
        target_size=1,
    )
    link_file = PatcherFile(
        name="link.bin",
        size=0,
        link="target",
        flags=None,
        chunks=[],
        manifest=manifest,
        chunk_hash_types={},
    )

    async def _fake_run_bundle_job(self, session, job, file_pool):
        raise DownloadError("mock failure")

    manifest._build_bundle_jobs = types.MethodType(
        lambda self, files: [BundleJob(bundle_id=chunk.bundle.bundle_id, ranges=[ChunkRange(start=0, end=0, tasks=[])])],
        manifest,
    )
    manifest._run_bundle_job_with_retry = types.MethodType(_fake_run_bundle_job, manifest)

    results = asyncio.run(manifest.download_files_concurrently([data_file, link_file], raise_on_error=False))
    assert results == (False, True)


def test_download_files_concurrently_empty_input_returns_empty_tuple(tmp_path: Path):
    manifest = _make_manifest_stub(tmp_path)
    assert asyncio.run(manifest.download_files_concurrently([])) == tuple()


def test_parse_flag_and_parameter_and_directory(monkeypatch):
    class _FlagParser:
        def __init__(self):
            self.skips = []

        def skip(self, amount: int):
            self.skips.append(amount)

        def unpack(self, fmt: str):
            assert fmt == "<xxxBl"
            return 5, 10

        def unpack_string(self):
            return "zh_CN"

    parser = _FlagParser()
    assert PatcherManifest._parse_flag(parser) == (5, "zh_CN")
    assert parser.skips == [4, 6]

    monkeypatch.setattr(PatcherManifest, "_parse_field_table", staticmethod(lambda p, f: {"hash_type": None}))
    assert PatcherManifest._parse_parameter(object()) == 0

    monkeypatch.setattr(
        PatcherManifest,
        "_parse_field_table",
        staticmethod(lambda p, f: {"name": "dir", "directory_id": 7, "parent_id": 3}),
    )
    assert PatcherManifest._parse_directory(object()) == ("dir", 7, 3)


def test_parse_field_table_reads_offset_and_string_values():
    entry_pos = 40
    fields_pos = 20
    payload = bytearray(128)

    payload[entry_pos : entry_pos + 4] = struct.pack("<l", entry_pos - fields_pos)
    payload[fields_pos : fields_pos + 2] = struct.pack("<H", 14)
    payload[fields_pos + 2 : fields_pos + 4] = struct.pack("<H", 0)
    payload[fields_pos + 4 : fields_pos + 14] = struct.pack("<5H", 4, 8, 12, 0, 16)

    payload[entry_pos + 4 : entry_pos + 8] = struct.pack("<L", 123)
    payload[entry_pos + 8 : entry_pos + 12] = struct.pack("<l", 20)
    payload[entry_pos + 12 : entry_pos + 16] = struct.pack("<l", 8)
    payload[entry_pos + 20 : entry_pos + 24] = struct.pack("<L", 3)
    payload[entry_pos + 24 : entry_pos + 27] = b"xyz"

    parser = BinaryParser(io.BytesIO(payload))
    parser.seek(entry_pos)

    fields = [
        ("plain", "<L"),
        ("offset_value", "offset"),
        ("text", "str"),
        ("zero_offset", "<L"),
        ("none_fmt", None),
        None,
        ("out_of_range", "<L"),
    ]
    parsed = PatcherManifest._parse_field_table(parser, fields)

    assert parsed == {
        "plain": 123,
        "offset_value": entry_pos + 8 + 20,
        "text": "xyz",
        "zero_offset": None,
        "none_fmt": None,
        "out_of_range": None,
    }
