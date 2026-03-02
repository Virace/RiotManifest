import types

import pytest
import pyzstd

from riotmanifest.extractor import WADExtractor
from riotmanifest.http_client import HttpClientError
from riotmanifest.manifest import DecompressError, DownloadError, PatcherBundle, PatcherFile, PatcherManifest


def _make_manifest_stub() -> PatcherManifest:
    manifest = object.__new__(PatcherManifest)
    manifest.file = "stub.manifest"
    manifest.path = ""
    manifest.bundle_url = "https://example.invalid/bundles/"
    manifest.files = {}

    def _validate_chunk_hash(self, chunk_data, chunk_id, hash_type):  # pylint: disable=unused-argument
        return None

    manifest._validate_chunk_hash = types.MethodType(_validate_chunk_hash, manifest)
    return manifest


def _make_single_chunk_wad_file(
    manifest: PatcherManifest,
    raw: bytes,
    target_size: int | None = None,
) -> tuple[PatcherFile, bytes]:
    compressed = pyzstd.compress(raw)
    bundle = PatcherBundle(0xAABB)
    bundle.add_chunk(
        chunk_id=0xCCDD,
        size=len(compressed),
        target_size=target_size if target_size is not None else len(raw),
    )
    wad_file = PatcherFile(
        name="DATA/FINAL/Test.wad.client",
        size=max(len(raw), target_size or 0),
        link="",
        flags=None,
        chunks=bundle.chunks,
        manifest=manifest,
        chunk_hash_types={},
    )
    return wad_file, compressed


def test_download_chunk_bytes_success_and_cache(monkeypatch):
    manifest = _make_manifest_stub()
    wad_file, compressed = _make_single_chunk_wad_file(manifest, raw=b"HELLO")
    chunk = wad_file.chunks[0]
    extractor = WADExtractor(manifest, cache_max_entries=8, cache_max_bytes=1024)

    calls = {"count": 0}

    def _fake_http_get_bytes(url, headers=None, timeout=None):  # pylint: disable=unused-argument
        calls["count"] += 1
        return compressed

    monkeypatch.setattr("riotmanifest.extractor.http_get_bytes", _fake_http_get_bytes)

    assert extractor._download_chunk_bytes(wad_file, chunk) == b"HELLO"
    assert extractor._download_chunk_bytes(wad_file, chunk) == b"HELLO"
    assert calls["count"] == 1


def test_download_chunk_bytes_http_error_raises(monkeypatch):
    manifest = _make_manifest_stub()
    wad_file, _ = _make_single_chunk_wad_file(manifest, raw=b"HELLO")
    chunk = wad_file.chunks[0]
    extractor = WADExtractor(manifest, retry_limit=2)

    def _always_fail(url, headers=None, timeout=None):  # pylint: disable=unused-argument
        raise HttpClientError("boom")

    monkeypatch.setattr("riotmanifest.extractor.http_get_bytes", _always_fail)
    with pytest.raises(DownloadError, match="下载 chunk 失败"):
        extractor._download_chunk_bytes(wad_file, chunk)


def test_download_chunk_bytes_target_size_mismatch(monkeypatch):
    manifest = _make_manifest_stub()
    wad_file, compressed = _make_single_chunk_wad_file(manifest, raw=b"HELLO", target_size=32)
    chunk = wad_file.chunks[0]
    extractor = WADExtractor(manifest)

    monkeypatch.setattr(
        "riotmanifest.extractor.http_get_bytes",
        lambda url, headers=None, timeout=None: compressed,
    )
    with pytest.raises(DecompressError, match="大小不匹配"):
        extractor._download_chunk_bytes(wad_file, chunk)


def test_get_wad_header_two_pass(monkeypatch):
    manifest = _make_manifest_stub()
    extractor = WADExtractor(manifest)
    calls = []

    class _FakeWadFile:
        size = 300

    class _FakeAnalyzer:
        def __init__(self, data):
            self.header_size = 512

    class _FakeWad:
        def __init__(self, data):
            self.data = data

    def _fake_read_range(self, wad_file, start, length):  # pylint: disable=unused-argument
        calls.append((start, length))
        return b"x" * length

    monkeypatch.setattr(WADExtractor, "_read_wad_file_range", _fake_read_range)
    monkeypatch.setattr("riotmanifest.extractor.WadHeaderAnalyzer", _FakeAnalyzer)
    monkeypatch.setattr("riotmanifest.extractor.WAD", _FakeWad)

    wad = extractor.get_wad_header(_FakeWadFile())
    assert calls == [(0, extractor.V3_HEADER_MINI_SIZE), (0, 300)]
    assert len(wad.data) == 300
