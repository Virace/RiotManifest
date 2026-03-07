import types
from pathlib import Path

import pytest

from riotmanifest.extractor import WADExtractor
from riotmanifest.game import (
    LeagueManifestResolver,
    LiveConfigNotFoundError,
)
from riotmanifest.game.metadata import first_value, parse_game_release, version_key
from riotmanifest.manifest import DecompressError, DownloadError, PatcherBundle, PatcherFile, PatcherManifest


def _make_manifest_stub() -> PatcherManifest:
    manifest = object.__new__(PatcherManifest)
    manifest.file = "stub.manifest"
    manifest.path = ""
    manifest.bundle_url = "https://example.invalid/bundles/"
    manifest.files = {}

    def _validate_chunk_hash(self, chunk_data, chunk_id, hash_type):  # pylint: disable=unused-argument
        return None

    manifest.validate_chunk_hash = types.MethodType(_validate_chunk_hash, manifest)
    return manifest


def _make_wad_file(
    manifest: PatcherManifest,
    *,
    name: str = "DATA/FINAL/Test.wad.client",
    chunk_specs: list[tuple[int, int, int]] | None = None,
    file_size: int | None = None,
) -> PatcherFile:
    specs = chunk_specs or [(0xCCDD, 8, 8)]
    bundle = PatcherBundle(0xAABB)
    for chunk_id, size, target_size in specs:
        bundle.add_chunk(chunk_id=chunk_id, size=size, target_size=target_size)

    size = file_size if file_size is not None else sum(chunk.target_size for chunk in bundle.chunks)
    wad_file = PatcherFile(
        name=name,
        size=size,
        link="",
        flags=None,
        chunks=bundle.chunks,
        manifest=manifest,
        chunk_hash_types={},
    )
    manifest.files[wad_file.name] = wad_file
    return wad_file


def _raise(exc: Exception):
    raise exc


def test_init_requires_manifest_instance():
    with pytest.raises(TypeError, match="PatcherManifest"):
        WADExtractor(123)  # type: ignore[arg-type]


def test_context_manager_clear_cache_and_close():
    manifest = _make_manifest_stub()
    extractor = WADExtractor(manifest, cache_max_entries=8, cache_max_bytes=1024)

    extractor._cache_put((1, 1), b"abc")
    assert extractor.cache_stats() == {"entries": 1, "bytes": 3}

    with extractor as same:
        assert same is extractor

    assert extractor.cache_stats() == {"entries": 0, "bytes": 0}


def test_cache_put_disabled_too_large_and_replace():
    manifest = _make_manifest_stub()

    disabled = WADExtractor(manifest, cache_max_entries=0, cache_max_bytes=1024)
    disabled._cache_put((1, 1), b"abc")
    assert disabled.cache_stats() == {"entries": 0, "bytes": 0}

    too_large = WADExtractor(manifest, cache_max_entries=8, cache_max_bytes=2)
    too_large._cache_put((1, 1), b"abc")
    assert too_large.cache_stats() == {"entries": 0, "bytes": 0}

    replaced = WADExtractor(manifest, cache_max_entries=8, cache_max_bytes=1024)
    replaced._cache_put((1, 1), b"abc")
    replaced._cache_put((1, 1), b"d")
    assert replaced.cache_stats() == {"entries": 1, "bytes": 1}


def test_prepare_prefetch_deduplicates_chunk_list(monkeypatch):
    manifest = _make_manifest_stub()
    wad_file = _make_wad_file(
        manifest,
        chunk_specs=[(0x1, 3, 3), (0x2, 3, 3)],
        file_size=6,
    )
    extractor = WADExtractor(
        manifest,
        prefetch_chunk_concurrency=4,
        recommended_max_targets_per_wad=10,
    )

    sections = [
        types.SimpleNamespace(offset=0, compressed_size=3),
        types.SimpleNamespace(offset=3, compressed_size=3),
        types.SimpleNamespace(offset=1, compressed_size=1),
    ]

    captured_chunk_ids: list[int] = []

    def _fake_prefetch(self, file, chunks):  # pylint: disable=unused-argument
        captured_chunk_ids.extend(chunk.chunk_id for chunk in chunks)

    monkeypatch.setattr(WADExtractor, "_prefetch_chunks", _fake_prefetch)
    extractor._prepare_prefetch(wad_file, sections)

    assert set(captured_chunk_ids) == {0x1, 0x2}
    assert len(captured_chunk_ids) == 2


def test_prepare_prefetch_skips_when_targets_too_many(monkeypatch):
    manifest = _make_manifest_stub()
    wad_file = _make_wad_file(
        manifest,
        chunk_specs=[(0x1, 3, 3), (0x2, 3, 3)],
        file_size=6,
    )
    extractor = WADExtractor(
        manifest,
        prefetch_chunk_concurrency=4,
        recommended_max_targets_per_wad=1,
    )

    sections = [
        types.SimpleNamespace(offset=0, compressed_size=3),
        types.SimpleNamespace(offset=3, compressed_size=3),
    ]

    state = {"called": False}

    def _fake_prefetch(self, file, chunks):  # pylint: disable=unused-argument
        state["called"] = True

    monkeypatch.setattr(WADExtractor, "_prefetch_chunks", _fake_prefetch)
    extractor._prepare_prefetch(wad_file, sections)

    assert not state["called"]


def test_extract_files_global_prefetch_deduplicates_cross_wad(monkeypatch):
    manifest = _make_manifest_stub()
    wad_file_a = _make_wad_file(
        manifest,
        name="DATA/FINAL/A.wad.client",
        chunk_specs=[(0x1, 2, 2), (0x2, 2, 2)],
        file_size=4,
    )
    wad_file_b = _make_wad_file(
        manifest,
        name="DATA/FINAL/B.wad.client",
        chunk_specs=[(0x2, 2, 2), (0x3, 2, 2)],
        file_size=4,
    )
    extractor = WADExtractor(
        manifest,
        prefetch_chunk_concurrency=4,
        recommended_max_targets_per_wad=10,
    )

    class _FakeHeader:
        def __init__(self, mapping: dict[str, int]):
            self._mapping = mapping
            self.files = [
                types.SimpleNamespace(path_hash=mapping["first.bin"], offset=0, compressed_size=2),
                types.SimpleNamespace(path_hash=mapping["second.bin"], offset=2, compressed_size=2),
            ]

        def _get_hash_for_path(self, path: str) -> int:
            return self._mapping[path]

        @staticmethod
        def extract_by_section(section, file_path, raw=False, data=None):  # pylint: disable=unused-argument
            return data

    headers = {
        wad_file_a.name: _FakeHeader({"first.bin": 0xA1, "second.bin": 0xA2}),
        wad_file_b.name: _FakeHeader({"first.bin": 0xB1, "second.bin": 0xB2}),
    }

    monkeypatch.setattr(WADExtractor, "get_wad_header", lambda self, file: headers[file.name])
    monkeypatch.setattr(
        WADExtractor,
        "_read_wad_file_range",
        lambda self, wad_file, start, length: b"x" * length,  # pylint: disable=unused-argument
    )

    captured_chunk_ids: list[int] = []
    state = {"called": 0}

    def _fake_prefetch_tasks(self, chunk_tasks):
        state["called"] += 1
        captured_chunk_ids.extend(task.chunk.chunk_id for task in chunk_tasks)

    monkeypatch.setattr(WADExtractor, "_prefetch_chunk_tasks", _fake_prefetch_tasks)

    result = extractor.extract_files(
        {
            wad_file_a.name: ["first.bin", "second.bin"],
            wad_file_b.name: ["first.bin", "second.bin"],
        }
    )

    assert result[wad_file_a.name]["first.bin"] == b"xx"
    assert result[wad_file_b.name]["second.bin"] == b"xx"
    assert state["called"] == 1
    assert set(captured_chunk_ids) == {0x1, 0x2, 0x3}
    assert len(captured_chunk_ids) == 3


def test_extract_files_global_prefetch_skips_wad_when_targets_too_many(monkeypatch):
    manifest = _make_manifest_stub()
    wad_file = _make_wad_file(
        manifest,
        name="DATA/FINAL/C.wad.client",
        chunk_specs=[(0x10, 2, 2), (0x11, 2, 2)],
        file_size=4,
    )
    extractor = WADExtractor(
        manifest,
        prefetch_chunk_concurrency=4,
        recommended_max_targets_per_wad=1,
    )

    class _FakeHeader:
        files = [
            types.SimpleNamespace(path_hash=0xC1, offset=0, compressed_size=2),
            types.SimpleNamespace(path_hash=0xC2, offset=2, compressed_size=2),
        ]

        @staticmethod
        def _get_hash_for_path(path: str) -> int:
            return 0xC1 if path == "first.bin" else 0xC2

        @staticmethod
        def extract_by_section(section, file_path, raw=False, data=None):  # pylint: disable=unused-argument
            return data

    monkeypatch.setattr(WADExtractor, "get_wad_header", lambda self, file: _FakeHeader())
    monkeypatch.setattr(
        WADExtractor,
        "_read_wad_file_range",
        lambda self, wad_file, start, length: b"x" * length,  # pylint: disable=unused-argument
    )

    state = {"called": False}

    def _fake_prefetch_tasks(self, chunk_tasks):  # pylint: disable=unused-argument
        state["called"] = True

    monkeypatch.setattr(WADExtractor, "_prefetch_chunk_tasks", _fake_prefetch_tasks)
    result = extractor.extract_files({wad_file.name: ["first.bin", "second.bin"]})

    assert result[wad_file.name]["first.bin"] == b"xx"
    assert result[wad_file.name]["second.bin"] == b"xx"
    assert not state["called"]


def test_download_chunk_bytes_zero_size_returns_empty(monkeypatch):
    manifest = _make_manifest_stub()
    wad_file = _make_wad_file(
        manifest,
        chunk_specs=[(0xCCDD, 0, 0)],
        file_size=0,
    )
    chunk = wad_file.chunks[0]
    extractor = WADExtractor(manifest)

    def _unexpected_call(url, headers=None, timeout=None):  # pylint: disable=unused-argument
        raise AssertionError("size=0 不应触发网络请求")

    monkeypatch.setattr("riotmanifest.extractor.wad_extractor.http_get_bytes", _unexpected_call)
    assert extractor._download_chunk_bytes(wad_file, chunk) == b""


def test_download_chunk_bytes_length_mismatch_raises(monkeypatch):
    manifest = _make_manifest_stub()
    wad_file = _make_wad_file(manifest, chunk_specs=[(0xCCDD, 5, 5)], file_size=5)
    chunk = wad_file.chunks[0]
    extractor = WADExtractor(manifest)

    monkeypatch.setattr(
        "riotmanifest.extractor.wad_extractor.http_get_bytes",
        lambda url, headers=None, timeout=None: b"1234",  # pylint: disable=unused-argument
    )

    with pytest.raises(DownloadError, match="actual=4"):
        extractor._download_chunk_bytes(wad_file, chunk)


def test_download_chunk_bytes_decompress_error_raises(monkeypatch):
    manifest = _make_manifest_stub()
    wad_file = _make_wad_file(manifest, chunk_specs=[(0xCCDD, 4, 4)], file_size=4)
    chunk = wad_file.chunks[0]
    extractor = WADExtractor(manifest)

    monkeypatch.setattr(
        "riotmanifest.extractor.wad_extractor.http_get_bytes",
        lambda url, headers=None, timeout=None: b"ABCD",  # pylint: disable=unused-argument
    )

    with pytest.raises(DecompressError, match="解压 chunk 失败"):
        extractor._download_chunk_bytes(wad_file, chunk)


def test_collect_chunks_for_range_covers_boundaries():
    manifest = _make_manifest_stub()
    wad_file = _make_wad_file(
        manifest,
        chunk_specs=[(0x1, 5, 5), (0x2, 5, 5)],
        file_size=10,
    )

    with pytest.raises(ValueError, match="无效区间"):
        WADExtractor._collect_chunks_for_range(wad_file, start=-1, length=1)

    assert WADExtractor._collect_chunks_for_range(wad_file, start=0, length=0) == ([], 0)

    with pytest.raises(ValueError, match="超出文件大小"):
        WADExtractor._collect_chunks_for_range(wad_file, start=0, length=11)

    selected, slice_start = WADExtractor._collect_chunks_for_range(wad_file, start=1, length=2)
    assert [chunk.chunk_id for chunk in selected] == [0x1]
    assert slice_start == 1

    empty_file = PatcherFile("empty.wad.client", 10, "", None, [], manifest, chunk_hash_types={})
    with pytest.raises(ValueError, match="未匹配到区间"):
        WADExtractor._collect_chunks_for_range(empty_file, start=0, length=1)


def test_read_wad_file_range_zero_and_slice_overflow(monkeypatch):
    manifest = _make_manifest_stub()
    wad_file = _make_wad_file(manifest, chunk_specs=[(0x1, 2, 2)], file_size=2)
    extractor = WADExtractor(manifest)

    assert extractor._read_wad_file_range(wad_file, start=0, length=0) == b""

    monkeypatch.setattr(
        WADExtractor,
        "_collect_chunks_for_range",
        lambda self, file, start, length: ([file.chunks[0]], 2),
    )
    monkeypatch.setattr(WADExtractor, "_download_chunk_bytes", lambda self, file, chunk: b"AB")

    with pytest.raises(DecompressError, match="切片越界"):
        extractor._read_wad_file_range(wad_file, start=0, length=3)


def test_resolve_hash_fallback_and_find_file_fallback(monkeypatch):
    manifest = _make_manifest_stub()
    wad_file = _make_wad_file(manifest, name="DATA/FINAL/Test.wad.client")
    extractor = WADExtractor(manifest)

    class _FallbackWAD:
        @staticmethod
        def get_hash(path: str) -> int:
            return 0x1234 if path == "x.bin" else 0x0

    monkeypatch.setattr("riotmanifest.extractor.wad_extractor.WAD", _FallbackWAD)
    assert extractor._resolve_path_hash(object(), "x.bin") == 0x1234

    manifest.filter_files = types.MethodType(lambda self, pattern=None, flag=None: [wad_file], manifest)
    assert extractor._find_wad_file("Test.wad.client") is wad_file

    manifest.filter_files = types.MethodType(lambda self, pattern=None, flag=None: [], manifest)
    assert extractor._find_wad_file("missing.wad.client") is None


def test_extract_files_handles_missing_and_header_error(monkeypatch):
    manifest = _make_manifest_stub()
    extractor = WADExtractor(manifest)

    missing = extractor.extract_files({"missing.wad.client": ["a.bin"]})
    assert missing["missing.wad.client"]["a.bin"] is None

    wad_file = _make_wad_file(manifest)
    monkeypatch.setattr(WADExtractor, "get_wad_header", lambda self, file: _raise(DownloadError("bad header")))

    header_error = extractor.extract_files({wad_file.name: ["a.bin"]})
    assert header_error[wad_file.name]["a.bin"] is None


def test_extract_files_handles_extract_error_and_disk_none(tmp_path: Path, monkeypatch):
    manifest = _make_manifest_stub()
    wad_file = _make_wad_file(manifest)
    extractor = WADExtractor(manifest)

    class _Section:
        path_hash = 0x11
        offset = 0
        compressed_size = 3

    class _ErrorHeader:
        def __init__(self):
            self.files = [_Section()]

        @staticmethod
        def _get_hash_for_path(path: str) -> int:
            return 0x11

        @staticmethod
        def extract_by_section(section, file_path, raw=False, data=None):  # pylint: disable=unused-argument
            return data

    monkeypatch.setattr(WADExtractor, "get_wad_header", lambda self, file: _ErrorHeader())

    def _bad_read_range(self, wad_file, start, length):  # pylint: disable=unused-argument
        return _raise(ValueError("bad range"))

    monkeypatch.setattr(WADExtractor, "_read_wad_file_range", _bad_read_range)

    extract_error = extractor.extract_files({wad_file.name: ["a/b.bin"]})
    assert extract_error[wad_file.name]["a/b.bin"] is None

    class _NoneDataHeader(_ErrorHeader):
        @staticmethod
        def extract_by_section(section, file_path, raw=False, data=None):  # pylint: disable=unused-argument
            return None

    monkeypatch.setattr(WADExtractor, "get_wad_header", lambda self, file: _NoneDataHeader())

    def _ok_read_range(self, wad_file, start, length):  # pylint: disable=unused-argument
        return b"XYZ"

    monkeypatch.setattr(WADExtractor, "_read_wad_file_range", _ok_read_range)

    outputs = extractor.extract_files_to_disk({wad_file.name: ["a/b.bin"]}, output_dir=str(tmp_path))
    assert outputs[wad_file.name]["a/b.bin"] is None

    monkeypatch.setattr(WADExtractor, "get_wad_header", lambda self, file: _ErrorHeader())
    escaped = extractor.extract_files_to_disk({wad_file.name: ["../escape.bin"]}, output_dir=str(tmp_path))
    assert escaped[wad_file.name]["../escape.bin"] is None


def test_build_disk_output_path_rejects_escape(tmp_path: Path):
    extractor = WADExtractor(_make_manifest_stub())

    with pytest.raises(ValueError, match="越界路径"):
        extractor._build_disk_output_path(tmp_path, "A.wad.client", "../../etc/passwd")

    with pytest.raises(ValueError, match="绝对路径"):
        extractor._build_disk_output_path(tmp_path, "A.wad.client", "/abs/path.bin")


def _make_game_release(version: str, url: str, artifact_type: str = "lol-game-client", platforms=None):
    platforms = platforms or ["windows"]
    return {
        "release": {
            "labels": {
                "riot:artifact_type_id": {"values": [artifact_type]},
                "platform": {"values": platforms},
                "riot:artifact_version_id": {"values": [version]},
            }
        },
        "download": {"url": url},
    }


def test_game_static_helpers_cover_branches():
    assert first_value([]) is None
    assert first_value("not-list") is None
    assert first_value([123]) == "123"
    assert version_key("14..A-B") == ((0, 14), (1, "a"), (1, "b"))


@pytest.mark.parametrize(
    "release",
    [
        _make_game_release("14.1.0+meta", "https://example.invalid/a.manifest", artifact_type="lcu"),
        _make_game_release("14.1.0+meta", "https://example.invalid/a.manifest", platforms=["mac"]),
        {
            "release": {
                "labels": {
                    "riot:artifact_type_id": {"values": ["lol-game-client"]},
                    "platform": {"values": ["windows"]},
                    "riot:artifact_version_id": {"values": []},
                }
            },
            "download": {"url": "https://example.invalid/a.manifest"},
        },
        {
            "release": {
                "labels": {
                    "riot:artifact_type_id": {"values": ["lol-game-client"]},
                    "platform": {"values": ["windows"]},
                    "riot:artifact_version_id": {"values": ["14.1.0"]},
                }
            },
            "download": {},
        },
    ],
)
def test_parse_game_release_filters_invalid_inputs(release):
    assert parse_game_release(release) is None


def test_load_lcu_data_non_dict_response(monkeypatch):
    monkeypatch.setattr("riotmanifest.game.metadata.http_get_json", lambda url: [])

    data = LeagueManifestResolver()
    data.load_lcu_data()

    assert data.available_lcu_regions() == []


def test_load_lcu_data_skips_invalid_configs(monkeypatch):
    def _fake_http_get_json(url: str):
        return {
            "league.live": {
                "platforms": {
                    "win": {
                        "configurations": [
                            {"id": "A", "patch_url": "https://example.invalid/a.manifest", "metadata": {"theme_manifest": 1}},
                            {
                                "id": "B",
                                "patch_url": "https://example.invalid/b.manifest",
                                "metadata": {"theme_manifest": "bad/path"},
                            },
                            {
                                "id": "EUW",
                                "patch_url": "https://example.invalid/euw.manifest",
                                "metadata": {"theme_manifest": "https://example.invalid/releases/14.4.1/theme/data"},
                            },
                        ]
                    }
                }
            }
        }

    monkeypatch.setattr("riotmanifest.game.metadata.http_get_json", _fake_http_get_json)

    data = LeagueManifestResolver()
    data.load_lcu_data()

    with pytest.warns(FutureWarning, match="latest_lcu\\(\\) 已弃用"):
        assert data.latest_lcu("EUW") == {"version": "14.4.1", "url": "https://example.invalid/euw.manifest"}


def test_load_game_data_skips_non_dict_release_and_available_regions(monkeypatch):
    def _fake_http_get_json(url: str):
        if "version-sets/KR" in url:
            return {
                "releases": [
                    123,
                    _make_game_release("14.2.1+meta", "https://example.invalid/kr-1421.manifest"),
                ]
            }
        return {
            "releases": [],
        }

    monkeypatch.setattr("riotmanifest.game.metadata.http_get_json", _fake_http_get_json)

    data = LeagueManifestResolver()
    data.load_game_data(regions=["KR", "EUW1"])

    with pytest.warns(FutureWarning, match="latest_game\\(\\) 已弃用"):
        assert data.latest_game("KR") == {"version": "14.2.1", "url": "https://example.invalid/kr-1421.manifest"}
    assert data.available_game_regions() == ["EUW1", "KR"]


def test_build_lcu_extractor_requires_live_region(monkeypatch):
    monkeypatch.setattr("riotmanifest.game.metadata.http_get_json", lambda url: {})

    data = LeagueManifestResolver()
    with pytest.raises(LiveConfigNotFoundError, match="EUW"):
        data.build_lcu_extractor("EUW")
