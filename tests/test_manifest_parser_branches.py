import asyncio
import io
import struct
import types

import aiohttp
import pytest
import pyzstd

from riotmanifest.manifest import ChunkRange, DownloadError, PatcherBundle, PatcherManifest


def _make_manifest_stub() -> PatcherManifest:
    manifest = object.__new__(PatcherManifest)
    manifest.file = "stub.manifest"
    manifest.path = ""
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


class _FakeResponse:
    def __init__(self, status: int, headers: dict[str, str], payload: bytes):
        self.status = status
        self.headers = headers
        self._payload = payload

    async def read(self):
        return self._payload


class _FakeResponseContext:
    def __init__(self, response: _FakeResponse):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False


class _FakeSession:
    def __init__(self, response: _FakeResponse):
        self._response = response
        self.last_request = None

    def get(self, url, headers=None, timeout=None):
        self.last_request = (url, headers, timeout)
        return _FakeResponseContext(self._response)


def test_parse_rman_dispatches_parse_body():
    manifest = _make_manifest_stub()
    raw_body = b"raw-manifest-body"
    compressed_body = pyzstd.compress(raw_body)

    payload = (
        struct.pack("<4sBB", b"RMAN", 2, 1)
        + struct.pack("<HLLQL", 1 << 9, 28, len(compressed_body), 0x1234, len(raw_body))
        + compressed_body
    )

    captured = {}

    def _fake_parse_body(self, f):
        captured["body"] = f.read()
        return "ok"

    manifest.parse_body = types.MethodType(_fake_parse_body, manifest)
    result = manifest.parse_rman(io.BytesIO(payload))

    assert result == "ok"
    assert captured["body"] == raw_body


def test_parse_rman_invalid_magic():
    manifest = _make_manifest_stub()
    payload = struct.pack("<4sBB", b"XXXX", 2, 1) + b"\x00" * 64
    with pytest.raises(ValueError, match="invalid magic code"):
        manifest.parse_rman(io.BytesIO(payload))


def test_parse_rman_invalid_version():
    manifest = _make_manifest_stub()
    payload = struct.pack("<4sBB", b"RMAN", 1, 0) + b"\x00" * 64
    with pytest.raises(ValueError, match="unsupported RMAN version"):
        manifest.parse_rman(io.BytesIO(payload))


def test_parse_rman_invalid_flags():
    manifest = _make_manifest_stub()
    payload = struct.pack("<4sBB", b"RMAN", 2, 1) + struct.pack("<HLLQL", 0, 28, 0, 0, 0)
    with pytest.raises(ValueError, match="unsupported RMAN flags"):
        manifest.parse_rman(io.BytesIO(payload))


def test_parse_rman_invalid_offset():
    manifest = _make_manifest_stub()
    payload = struct.pack("<4sBB", b"RMAN", 2, 1) + struct.pack("<HLLQL", 1 << 9, 27, 0, 0, 0)
    with pytest.raises(ValueError, match="invalid RMAN body offset"):
        manifest.parse_rman(io.BytesIO(payload))


def test_parse_body_builds_files_and_hash_types(monkeypatch):
    manifest = _make_manifest_stub()
    bundle = PatcherBundle(0x1001)
    bundle.add_chunk(chunk_id=0x2002, size=8, target_size=16)

    file_entries = [
        ("a.bin", "", [1], None, 16, [0x2002], 0),
        ("b.bin", "", None, 10, 16, [0x2002], 99),
    ]
    directories = [("dir", 10, None)]

    def _fake_parse_table(parser, entry_parser):
        name = entry_parser.__name__
        if name == "_parse_bundle":
            return [bundle]
        if name == "_parse_flag":
            return [(1, "zh_CN")]
        if name == "_parse_file_entry":
            return file_entries
        if name == "_parse_directory":
            return directories
        if name == "_parse_parameter":
            return [7]
        raise AssertionError(f"unexpected parser name: {name}")

    monkeypatch.setattr(PatcherManifest, "_parse_table", staticmethod(_fake_parse_table))
    body = struct.pack("<l", 0) + struct.pack("<6l", 0, 0, 0, 0, 0, 0) + b"\x00" * 128
    manifest.parse_body(io.BytesIO(body))

    assert "a.bin" in manifest.files
    assert "dir/b.bin" in manifest.files
    assert manifest.files["a.bin"].flags == ["zh_CN"]
    assert manifest.files["dir/b.bin"].flags is None
    assert manifest.files["a.bin"].chunk_hash_types[0x2002] == 7
    assert manifest.files["dir/b.bin"].chunk_hash_types[0x2002] == 0


def test_parse_table_iterates_by_offset():
    class _DummyParser:
        def __init__(self):
            self.pos = 100
            self.unpack_values = [(2,), (8,), (10,)]

        def tell(self):
            return self.pos

        def unpack(self, fmt):  # pylint: disable=unused-argument
            return self.unpack_values.pop(0)

        def seek(self, position):
            self.pos = position

    parser = _DummyParser()
    values = list(PatcherManifest._parse_table(parser, lambda p: p.tell()))
    assert values == [108, 114]


def test_parse_bundle_builds_chunk(monkeypatch):
    class _DummyParser:
        def __init__(self):
            self.seeks = []

        def seek(self, position):
            self.seeks.append(position)

    parser = _DummyParser()

    def _fake_parse_field_table(_, fields):
        if fields[0] and fields[0][0] == "bundle_id":
            return {"bundle_id": 0xA0, "chunks_offset": 77}
        return {"chunk_id": 0xB0, "compressed_size": 12, "uncompressed_size": 34}

    def _fake_parse_table(_parser, entry_parser):
        return [entry_parser(_parser)]

    monkeypatch.setattr(PatcherManifest, "_parse_field_table", staticmethod(_fake_parse_field_table))
    monkeypatch.setattr(PatcherManifest, "_parse_table", staticmethod(_fake_parse_table))

    bundle = PatcherManifest._parse_bundle(parser)
    assert parser.seeks == [77]
    assert bundle.bundle_id == 0xA0
    assert len(bundle.chunks) == 1
    assert bundle.chunks[0].chunk_id == 0xB0
    assert bundle.chunks[0].size == 12
    assert bundle.chunks[0].target_size == 34


def test_parse_file_entry_with_flags(monkeypatch):
    class _DummyParser:
        def __init__(self):
            self.seeks = []

        def seek(self, position):
            self.seeks.append(position)

        def unpack(self, fmt):
            if fmt == "<L":
                return (2,)
            if fmt == "<2Q":
                return (0x11, 0x22)
            raise AssertionError(f"unexpected fmt: {fmt}")

    parser = _DummyParser()

    monkeypatch.setattr(
        PatcherManifest,
        "_parse_field_table",
        staticmethod(
            lambda _p, _fields: {
                "name": "x.bin",
                "link": "",
                "flags": 0b101,
                "directory_id": None,
                "file_size": 123,
                "chunks": 999,
                "param_index": None,
            }
        ),
    )

    name, link, flag_ids, dir_id, size, chunk_ids, param_index = PatcherManifest._parse_file_entry(parser)
    assert parser.seeks == [999]
    assert name == "x.bin"
    assert link == ""
    assert flag_ids == [1, 3]
    assert dir_id is None
    assert size == 123
    assert chunk_ids == [0x11, 0x22]
    assert param_index is None


def test_parse_multipart_response_maps_by_content_range(monkeypatch):
    manifest = _make_manifest_stub()

    class _Part:
        def __init__(self, content_range: str, payload: bytes):
            self.headers = {aiohttp.hdrs.CONTENT_RANGE: content_range}
            self._payload = payload

        async def read(self, decode=False):  # pylint: disable=unused-argument
            return self._payload

    class _Reader:
        def __init__(self):
            self.parts = [
                _Part("bytes 3-5/6", b"DEF"),
                _Part("bytes 0-2/6", b"ABC"),
            ]

        async def next(self):
            if not self.parts:
                return None
            return self.parts.pop(0)

    class _FakeMultipartReader:
        @staticmethod
        def from_response(response):  # pylint: disable=unused-argument
            return _Reader()

    monkeypatch.setattr("riotmanifest.manifest.aiohttp.MultipartReader", _FakeMultipartReader)

    ranges = [
        ChunkRange(start=0, end=2, tasks=[]),
        ChunkRange(start=3, end=5, tasks=[]),
    ]
    result = asyncio.run(manifest._parse_multipart_response(object(), ranges, 0x1))
    assert result == [b"ABC", b"DEF"]


def test_parse_multipart_response_missing_part_raises(monkeypatch):
    manifest = _make_manifest_stub()

    class _Part:
        def __init__(self, content_range: str, payload: bytes):
            self.headers = {aiohttp.hdrs.CONTENT_RANGE: content_range}
            self._payload = payload

        async def read(self, decode=False):  # pylint: disable=unused-argument
            return self._payload

    class _Reader:
        def __init__(self):
            self.parts = [_Part("bytes 0-2/6", b"ABC")]

        async def next(self):
            if not self.parts:
                return None
            return self.parts.pop(0)

    class _FakeMultipartReader:
        @staticmethod
        def from_response(response):  # pylint: disable=unused-argument
            return _Reader()

    monkeypatch.setattr("riotmanifest.manifest.aiohttp.MultipartReader", _FakeMultipartReader)
    ranges = [ChunkRange(start=0, end=2, tasks=[]), ChunkRange(start=3, end=5, tasks=[])]
    with pytest.raises(DownloadError, match="multipart段数不足"):
        asyncio.run(manifest._parse_multipart_response(object(), ranges, 0x1))


def test_fetch_ranges_data_status_200(monkeypatch):
    manifest = _make_manifest_stub()
    response = _FakeResponse(status=200, headers={}, payload=b"ABC")
    session = _FakeSession(response)
    ranges = [ChunkRange(start=0, end=2, tasks=[])]

    def _fake_extract(payload, target_ranges, bundle_id):  # pylint: disable=unused-argument
        assert payload == b"ABC"
        assert len(target_ranges) == 1
        return [b"ABC"]

    monkeypatch.setattr(PatcherManifest, "_extract_ranges_from_full_body", staticmethod(_fake_extract))
    result = asyncio.run(manifest._fetch_ranges_data(session, 0x1234, ranges))
    assert result == [b"ABC"]
    assert session.last_request is not None
    assert session.last_request[1]["Range"] == "bytes=0-2"


def test_fetch_ranges_data_status_206_single_range_plain():
    manifest = _make_manifest_stub()
    response = _FakeResponse(status=206, headers={aiohttp.hdrs.CONTENT_TYPE: "application/octet-stream"}, payload=b"ABC")
    session = _FakeSession(response)
    ranges = [ChunkRange(start=0, end=2, tasks=[])]

    result = asyncio.run(manifest._fetch_ranges_data(session, 0x1234, ranges))
    assert result == [b"ABC"]


def test_fetch_ranges_data_status_206_multi_range_plain_raises():
    manifest = _make_manifest_stub()
    response = _FakeResponse(status=206, headers={aiohttp.hdrs.CONTENT_TYPE: "application/octet-stream"}, payload=b"ABC")
    session = _FakeSession(response)
    ranges = [
        ChunkRange(start=0, end=0, tasks=[]),
        ChunkRange(start=1, end=1, tasks=[]),
    ]

    with pytest.raises(DownloadError, match="多段range未返回multipart"):
        asyncio.run(manifest._fetch_ranges_data(session, 0x1234, ranges))
