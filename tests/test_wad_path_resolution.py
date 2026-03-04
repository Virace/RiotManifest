"""WAD 路径回填能力测试."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

import riotmanifest.diff.wad_path_resolution as wad_path_resolution
from riotmanifest.diff import (
    ManifestBinPathProvider,
    WADHeaderDiffReport,
    diff_wad_headers,
    resolve_wad_diff_paths,
)
from riotmanifest.extractor.wad_extractor import WADExtractor
from riotmanifest.manifest import PatcherBundle, PatcherFile, PatcherManifest


def _make_manifest_stub(name: str) -> PatcherManifest:
    manifest = object.__new__(PatcherManifest)
    manifest.file = f"{name}.manifest"
    manifest.path = ""
    manifest.bundle_url = "https://example.invalid/bundles/"
    manifest.bundles = []
    manifest.chunks = {}
    manifest.flags = {}
    manifest.files = {}
    return manifest


def _add_file(
    manifest: PatcherManifest,
    *,
    name: str,
    chunk_ids: list[int],
    flags: list[str] | None = None,
    bundle_id: int = 0x1001,
) -> PatcherFile:
    bundle = PatcherBundle(bundle_id)
    for chunk_id in chunk_ids:
        bundle.add_chunk(chunk_id=chunk_id, size=1, target_size=1)
    file = PatcherFile(
        name=name,
        size=max(1, len(chunk_ids)),
        link="",
        flags=flags,
        chunks=bundle.chunks,
        manifest=manifest,
        chunk_hash_types={chunk_id: 0 for chunk_id in chunk_ids},
    )
    manifest.files[name] = file
    return file


class _StaticPathProvider:
    """静态 BIN 种子路径提供器（测试用）."""

    def __init__(self, mapping: dict[str, tuple[str, ...]]):
        self.mapping = mapping

    def collect_paths(self, wad_path: str) -> tuple[str, ...]:
        return self.mapping.get(wad_path.lower(), tuple())


@dataclass
class _FakeSection:
    path_hash: int
    offset: int
    compressed_size: int
    size: int
    type: int


class _FakeHeader:
    """最小 WAD 头桩对象."""

    def __init__(self, sections: list[_FakeSection], path_map: dict[str, int]):
        self.files = sections
        self.path_map = {path.lower(): value for path, value in path_map.items()}

    def _get_hash_for_path(self, path: str) -> int:
        return self.path_map.get(path.lower(), 0xFFFFFFFFFFFFFFFF)


@dataclass
class _FakeBankUnit:
    bank_path: list[str]


@dataclass
class _FakeBinNode:
    bank_units: list[_FakeBankUnit]


class _FakeBIN:
    """最小 BIN 解析桩，模拟 `data.bank_units.bank_path` 结构."""

    def __init__(self, data: bytes):
        if not data:
            self.data = []
            return
        self.data = [
            _FakeBinNode(
                bank_units=[
                    _FakeBankUnit(
                        bank_path=[
                            "ASSETS/Sounds/Focus.bnk",
                            "ASSETS/Sounds/Unmatched.bnk",
                        ]
                    )
                ]
            )
        ]


def test_resolve_wad_diff_paths_backfills_section_path_from_bin(monkeypatch):
    old_manifest = _make_manifest_stub("old")
    new_manifest = _make_manifest_stub("new")
    wad_path = "DATA/FINAL/Champions/Aatrox.zh_CN.wad.client"
    root_wad_path = "DATA/FINAL/Champions/Aatrox.wad.client"

    _add_file(old_manifest, name=wad_path, chunk_ids=[0x1101], flags=["zh_CN"], bundle_id=0x2101)
    _add_file(new_manifest, name=wad_path, chunk_ids=[0x2202], flags=["zh_CN"], bundle_id=0x3202)
    _add_file(old_manifest, name=root_wad_path, chunk_ids=[0x3303], flags=["en_US"], bundle_id=0x4101)
    _add_file(new_manifest, name=root_wad_path, chunk_ids=[0x4404], flags=["en_US"], bundle_id=0x5202)

    headers = {
        ("old.manifest", wad_path): _FakeHeader(
            sections=[
                _FakeSection(path_hash=0x1, offset=0, compressed_size=10, size=20, type=3),
                _FakeSection(path_hash=0x2, offset=10, compressed_size=8, size=16, type=3),
            ],
            path_map={"ASSETS/Sounds/Focus.bnk": 0x1},
        ),
        ("new.manifest", wad_path): _FakeHeader(
            sections=[
                _FakeSection(path_hash=0x1, offset=0, compressed_size=11, size=20, type=3),
                _FakeSection(path_hash=0x3, offset=11, compressed_size=9, size=18, type=3),
            ],
            path_map={"ASSETS/Sounds/Focus.bnk": 0x1},
        ),
    }

    def _fake_get_wad_header(self: WADExtractor, wad_file: PatcherFile):
        return headers[(self.manifest.file, wad_file.name)]

    def _fake_extract_files(self: WADExtractor, wad_file_paths: dict[str, list[str]]):
        wad_name = next(iter(wad_file_paths))
        assert wad_name == root_wad_path
        return {wad_name: {path: b"BIN_BYTES" for path in wad_file_paths[wad_name]}}

    monkeypatch.setattr(WADExtractor, "get_wad_header", _fake_get_wad_header)
    monkeypatch.setattr(WADExtractor, "extract_files", _fake_extract_files)
    monkeypatch.setattr(wad_path_resolution, "BIN", _FakeBIN)

    wad_report = diff_wad_headers(
        old_manifest,
        new_manifest,
        target_wad_files=[wad_path],
        include_unchanged=True,
    )
    provider = _StaticPathProvider(
        {root_wad_path.lower(): ("data/characters/aatrox/skins/skin0.bin",)}
    )
    resolved_report = resolve_wad_diff_paths(wad_report, path_provider=provider)

    assert isinstance(resolved_report, WADHeaderDiffReport)
    assert resolved_report.summary == wad_report.summary

    original_entries = {entry.path_hash: entry for entry in wad_report.files[0].section_diffs}
    resolved_entries = {entry.path_hash: entry for entry in resolved_report.files[0].section_diffs}

    assert original_entries[0x1].path is None
    assert resolved_entries[0x1].path == "ASSETS/Sounds/Focus.bnk"
    assert resolved_entries[0x2].path is None
    assert resolved_entries[0x3].path is None

    resolved_manifest_entry = resolved_report.manifest_report.changed[0]
    assert resolved_manifest_entry.path == wad_path
    assert resolved_manifest_entry.section_diffs is not None
    manifest_sections = {entry.path_hash: entry for entry in resolved_manifest_entry.section_diffs}
    assert manifest_sections[0x1].path == "ASSETS/Sounds/Focus.bnk"


def test_resolve_wad_diff_paths_requires_manifest_context(monkeypatch):
    old_manifest = _make_manifest_stub("old")
    new_manifest = _make_manifest_stub("new")
    wad_path = "DATA/FINAL/Champions/Test.zh_CN.wad.client"

    _add_file(old_manifest, name=wad_path, chunk_ids=[0x1010], flags=["zh_CN"])
    _add_file(new_manifest, name=wad_path, chunk_ids=[0x2020], flags=["zh_CN"])

    headers = {
        ("old.manifest", wad_path): _FakeHeader(
            sections=[_FakeSection(path_hash=0x1, offset=0, compressed_size=10, size=10, type=3)],
            path_map={"ASSETS/Sounds/Focus.bnk": 0x1},
        ),
        ("new.manifest", wad_path): _FakeHeader(
            sections=[_FakeSection(path_hash=0x1, offset=0, compressed_size=11, size=10, type=3)],
            path_map={"ASSETS/Sounds/Focus.bnk": 0x1},
        ),
    }

    def _fake_get_wad_header(self: WADExtractor, wad_file: PatcherFile):
        return headers[(self.manifest.file, wad_file.name)]

    monkeypatch.setattr(WADExtractor, "get_wad_header", _fake_get_wad_header)

    wad_report = diff_wad_headers(
        old_manifest,
        new_manifest,
        target_wad_files=[wad_path],
        include_unchanged=True,
    )
    object.__setattr__(wad_report.manifest_report, "_old_manifest_obj", None)
    object.__setattr__(wad_report.manifest_report, "_new_manifest_obj", None)

    provider = _StaticPathProvider({wad_path.lower(): ("data/characters/test/skins/skin0.bin",)})
    with pytest.raises(ValueError, match="Manifest 上下文"):
        resolve_wad_diff_paths(wad_report, path_provider=provider)


def test_resolve_wad_diff_paths_download_root_wad_mode_uses_local_wad_and_cleanup(
    monkeypatch,
    tmp_path: Path,
):
    old_manifest = _make_manifest_stub("old")
    new_manifest = _make_manifest_stub("new")
    wad_path = "DATA/FINAL/Champions/Aatrox.zh_CN.wad.client"
    root_wad_path = "DATA/FINAL/Champions/Aatrox.wad.client"

    _add_file(old_manifest, name=wad_path, chunk_ids=[0x1101], flags=["zh_CN"], bundle_id=0x2101)
    _add_file(new_manifest, name=wad_path, chunk_ids=[0x2202], flags=["zh_CN"], bundle_id=0x3202)
    _add_file(old_manifest, name=root_wad_path, chunk_ids=[0x3303], flags=["en_US"], bundle_id=0x4101)
    _add_file(new_manifest, name=root_wad_path, chunk_ids=[0x4404], flags=["en_US"], bundle_id=0x5202)

    headers = {
        ("old.manifest", wad_path): _FakeHeader(
            sections=[_FakeSection(path_hash=0x1, offset=0, compressed_size=10, size=20, type=3)],
            path_map={"ASSETS/Sounds/Focus.bnk": 0x1},
        ),
        ("new.manifest", wad_path): _FakeHeader(
            sections=[_FakeSection(path_hash=0x1, offset=0, compressed_size=11, size=20, type=3)],
            path_map={"ASSETS/Sounds/Focus.bnk": 0x1},
        ),
    }

    def _fake_get_wad_header(self: WADExtractor, wad_file: PatcherFile):
        return headers[(self.manifest.file, wad_file.name)]

    calls = {"extractor_extract_files": 0, "downloads": 0}

    def _fake_extract_files(self: WADExtractor, wad_file_paths: dict[str, list[str]]):
        calls["extractor_extract_files"] += 1
        return {wad_name: {path: b"" for path in paths} for wad_name, paths in wad_file_paths.items()}

    async def _fake_download_files_concurrently(
        self: PatcherManifest,  # noqa: ARG001
        files: list[PatcherFile],
        concurrency_limit: int | None = None,  # noqa: ARG001
        raise_on_error: bool = True,  # noqa: ARG001
        progress_callback=None,  # noqa: ANN001, ARG001
        progress_interval_seconds: float | None = 1.0,  # noqa: ARG001
    ) -> tuple[bool, ...]:
        calls["downloads"] += 1
        output_root = Path(self.path)
        for file in files:
            output_path = output_root / file.name
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"LOCAL_WAD")
        return tuple(True for _ in files)

    class _FakeLocalWAD:
        def __init__(self, data: str | Path):
            assert Path(data).is_file()

        def extract(self, paths: list[str], out_dir: str = "", raw: bool = False):  # noqa: ARG002
            assert raw is True
            return [b"BIN_BYTES" if path.endswith("skin0.bin") else None for path in paths]

    monkeypatch.setattr(WADExtractor, "get_wad_header", _fake_get_wad_header)
    monkeypatch.setattr(WADExtractor, "extract_files", _fake_extract_files)
    monkeypatch.setattr(PatcherManifest, "download_files_concurrently", _fake_download_files_concurrently)
    monkeypatch.setattr(wad_path_resolution, "WAD", _FakeLocalWAD)
    monkeypatch.setattr(wad_path_resolution, "BIN", _FakeBIN)

    wad_report = diff_wad_headers(
        old_manifest,
        new_manifest,
        target_wad_files=[wad_path],
        include_unchanged=True,
    )
    provider = _StaticPathProvider(
        {root_wad_path.lower(): ("data/characters/aatrox/skins/skin0.bin",)}
    )
    cache_root = tmp_path / "cache"
    resolved_report = resolve_wad_diff_paths(
        wad_report,
        path_provider=provider,
        bin_data_source_mode="download_root_wad",
        root_wad_download_dir=cache_root,
        cleanup_downloaded_root_wads=True,
    )

    resolved_entry = resolved_report.files[0].section_diffs[0]
    assert resolved_entry.path == "ASSETS/Sounds/Focus.bnk"
    assert calls["downloads"] == 2
    assert calls["extractor_extract_files"] == 0
    assert list(cache_root.rglob("*.wad.client")) == []


def test_resolve_wad_diff_paths_download_root_wad_mode_can_keep_downloaded_files(
    monkeypatch,
    tmp_path: Path,
):
    old_manifest = _make_manifest_stub("old")
    new_manifest = _make_manifest_stub("new")
    wad_path = "DATA/FINAL/Champions/Aatrox.zh_CN.wad.client"
    root_wad_path = "DATA/FINAL/Champions/Aatrox.wad.client"

    _add_file(old_manifest, name=wad_path, chunk_ids=[0x1101], flags=["zh_CN"], bundle_id=0x2101)
    _add_file(new_manifest, name=wad_path, chunk_ids=[0x2202], flags=["zh_CN"], bundle_id=0x3202)
    _add_file(old_manifest, name=root_wad_path, chunk_ids=[0x3303], flags=["en_US"], bundle_id=0x4101)
    _add_file(new_manifest, name=root_wad_path, chunk_ids=[0x4404], flags=["en_US"], bundle_id=0x5202)

    headers = {
        ("old.manifest", wad_path): _FakeHeader(
            sections=[_FakeSection(path_hash=0x1, offset=0, compressed_size=10, size=20, type=3)],
            path_map={"ASSETS/Sounds/Focus.bnk": 0x1},
        ),
        ("new.manifest", wad_path): _FakeHeader(
            sections=[_FakeSection(path_hash=0x1, offset=0, compressed_size=11, size=20, type=3)],
            path_map={"ASSETS/Sounds/Focus.bnk": 0x1},
        ),
    }

    def _fake_get_wad_header(self: WADExtractor, wad_file: PatcherFile):
        return headers[(self.manifest.file, wad_file.name)]

    async def _fake_download_files_concurrently(
        self: PatcherManifest,  # noqa: ARG001
        files: list[PatcherFile],
        concurrency_limit: int | None = None,  # noqa: ARG001
        raise_on_error: bool = True,  # noqa: ARG001
        progress_callback=None,  # noqa: ANN001, ARG001
        progress_interval_seconds: float | None = 1.0,  # noqa: ARG001
    ) -> tuple[bool, ...]:
        output_root = Path(self.path)
        for file in files:
            output_path = output_root / file.name
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"LOCAL_WAD")
        return tuple(True for _ in files)

    class _FakeLocalWAD:
        def __init__(self, data: str | Path):
            assert Path(data).is_file()

        def extract(self, paths: list[str], out_dir: str = "", raw: bool = False):  # noqa: ARG002
            assert raw is True
            return [b"BIN_BYTES" if path.endswith("skin0.bin") else None for path in paths]

    monkeypatch.setattr(WADExtractor, "get_wad_header", _fake_get_wad_header)
    monkeypatch.setattr(PatcherManifest, "download_files_concurrently", _fake_download_files_concurrently)
    monkeypatch.setattr(wad_path_resolution, "WAD", _FakeLocalWAD)
    monkeypatch.setattr(wad_path_resolution, "BIN", _FakeBIN)

    wad_report = diff_wad_headers(
        old_manifest,
        new_manifest,
        target_wad_files=[wad_path],
        include_unchanged=True,
    )
    provider = _StaticPathProvider(
        {root_wad_path.lower(): ("data/characters/aatrox/skins/skin0.bin",)}
    )
    cache_root = tmp_path / "keep-cache"
    resolved_report = resolve_wad_diff_paths(
        wad_report,
        path_provider=provider,
        bin_data_source_mode="download_root_wad",
        root_wad_download_dir=cache_root,
        cleanup_downloaded_root_wads=False,
    )

    resolved_entry = resolved_report.files[0].section_diffs[0]
    assert resolved_entry.path == "ASSETS/Sounds/Focus.bnk"
    kept_files = list(cache_root.rglob("*.wad.client"))
    assert len(kept_files) == 2


def test_manifest_bin_path_provider_builds_bin_seed_paths_and_cache():
    manifest = _make_manifest_stub("provider")
    champion_wad = "DATA/FINAL/Champions/Aatrox.zh_CN.wad.client"
    map_wad = "DATA/FINAL/Maps/Shipping/Map11/Map11.wad.client"
    map_levels_wad = "DATA/FINAL/Maps/Shipping/Map11/Map11LEVELS.wad.client"

    provider = ManifestBinPathProvider(
        manifest=manifest,
        wad_bin_paths={
            "*": ("data/shared/global.bin",),
            champion_wad: ("data/custom/override.bin",),
        },
        max_skin_id=3,
    )

    champion_paths_first = provider.collect_paths(champion_wad)
    champion_paths_second = provider.collect_paths(champion_wad)
    map_paths = provider.collect_paths(map_wad)
    map_levels_paths = provider.collect_paths(map_levels_wad)
    provider.close()

    assert champion_paths_first == champion_paths_second
    assert "data/characters/aatrox/skins/skin0.bin" in champion_paths_first
    assert "data/characters/aatrox/skins/skin3.bin" in champion_paths_first
    assert "data/characters/aatrox/skins/root.bin" in champion_paths_first
    assert "data/characters/aatrox/aatrox.bin" in champion_paths_first
    assert "data/custom/override.bin" in champion_paths_first
    assert "data/shared/global.bin" in champion_paths_first

    assert "data/maps/shipping/common/common.bin" in map_paths
    assert "data/maps/shipping/map11/map11.bin" in map_paths
    assert "data/shared/global.bin" in map_paths

    assert "data/maps/shipping/map11/map11.bin" in map_levels_paths
    assert "data/maps/shipping/map11/map11levels.bin" in map_levels_paths
