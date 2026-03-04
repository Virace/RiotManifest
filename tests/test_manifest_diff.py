"""Manifest/WAD 差异分析能力测试."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from riotmanifest.diff import diff_manifests, diff_wad_headers
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
    size: int | None = None,
    flags: list[str] | None = None,
    link: str = "",
    bundle_id: int = 0x1001,
) -> PatcherFile:
    bundle = PatcherBundle(bundle_id)
    for chunk_id in chunk_ids:
        bundle.add_chunk(chunk_id=chunk_id, size=1, target_size=1)
    file_size = size if size is not None else max(1, len(chunk_ids))
    file = PatcherFile(
        name=name,
        size=file_size,
        link=link,
        flags=flags,
        chunks=bundle.chunks,
        manifest=manifest,
        chunk_hash_types={chunk_id: 0 for chunk_id in chunk_ids},
    )
    manifest.files[name] = file
    return file


def test_diff_manifests_detects_changed_added_removed_and_move():
    old_manifest = _make_manifest_stub("old")
    new_manifest = _make_manifest_stub("new")

    _add_file(old_manifest, name="DATA/keep.bin", chunk_ids=[0x01], bundle_id=0x2001)
    _add_file(old_manifest, name="DATA/change.bin", chunk_ids=[0x02], bundle_id=0x2002)
    _add_file(old_manifest, name="DATA/remove.bin", chunk_ids=[0x03], bundle_id=0x2003)
    _add_file(old_manifest, name="DATA/moved/old_path.bin", chunk_ids=[0x04], bundle_id=0x2004)

    _add_file(new_manifest, name="DATA/keep.bin", chunk_ids=[0x01], bundle_id=0x3001)
    _add_file(new_manifest, name="DATA/change.bin", chunk_ids=[0x22], bundle_id=0x3002)
    _add_file(new_manifest, name="DATA/add.bin", chunk_ids=[0x33], bundle_id=0x3003)
    _add_file(new_manifest, name="DATA/moved/new_path.bin", chunk_ids=[0x04], bundle_id=0x3004)

    report = diff_manifests(old_manifest, new_manifest, include_unchanged=True)

    assert {item.path for item in report.added} == {"DATA/add.bin", "DATA/moved/new_path.bin"}
    assert {item.path for item in report.removed} == {"DATA/remove.bin", "DATA/moved/old_path.bin"}
    assert {item.path for item in report.changed} == {"DATA/change.bin"}
    assert {item.path for item in report.unchanged} == {"DATA/keep.bin"}

    assert report.moved == (
        type(report.moved[0])(
            old_path="DATA/moved/old_path.bin",
            new_path="DATA/moved/new_path.bin",
            size=1,
            chunk_digest=report.moved[0].chunk_digest,
        ),
    )


def test_diff_manifests_supports_flags_and_targets():
    old_manifest = _make_manifest_stub("old")
    new_manifest = _make_manifest_stub("new")

    _add_file(old_manifest, name="DATA/common.bin", chunk_ids=[0x10], bundle_id=0x4001)
    _add_file(new_manifest, name="DATA/common.bin", chunk_ids=[0x10], bundle_id=0x5001)

    _add_file(
        old_manifest,
        name="DATA/lang.zh.bin",
        chunk_ids=[0x11],
        flags=["zh_CN"],
        bundle_id=0x4002,
    )
    _add_file(
        new_manifest,
        name="DATA/lang.zh.bin",
        chunk_ids=[0x22],
        flags=["zh_CN"],
        bundle_id=0x5002,
    )
    _add_file(
        old_manifest,
        name="DATA/lang.en.bin",
        chunk_ids=[0x33],
        flags=["en_US"],
        bundle_id=0x4003,
    )
    _add_file(
        new_manifest,
        name="DATA/lang.en.bin",
        chunk_ids=[0x33],
        flags=["en_US"],
        bundle_id=0x5003,
    )

    zh_report = diff_manifests(old_manifest, new_manifest, flags="zh_CN")
    assert {item.path for item in zh_report.changed} == {"DATA/lang.zh.bin"}
    assert not zh_report.added
    assert not zh_report.removed

    target_report = diff_manifests(
        old_manifest,
        new_manifest,
        target_files=["DATA/common.bin", "DATA/missing.bin"],
    )
    assert {item.path for item in target_report.unchanged} == {"DATA/common.bin"}
    assert any("missing.bin" in warning for warning in target_report.summary.warnings)


def test_diff_manifests_target_files_override_pattern_and_flags():
    old_manifest = _make_manifest_stub("old")
    new_manifest = _make_manifest_stub("new")

    _add_file(old_manifest, name="DATA/FINAL/Champions/Renata.zh_CN.wad.client", chunk_ids=[0x7001], bundle_id=0x9101)
    _add_file(new_manifest, name="DATA/FINAL/Champions/Renata.zh_CN.wad.client", chunk_ids=[0x7001], bundle_id=0x9201)

    report = diff_manifests(
        old_manifest,
        new_manifest,
        target_files=["DATA/FINAL/Champions/Renata.zh_CN.wad.client"],
        # 故意给错误过滤条件，验证 target_files 优先级。
        flags="en_US",
        pattern=r"^NOT_MATCH$",
        include_unchanged=True,
        filter_source="old",
    )
    assert report.summary.total_common == 1
    assert report.summary.changed_count == 0
    assert report.summary.unchanged_count == 1
    assert any("filter_source 将被忽略" in warning for warning in report.summary.warnings)


def test_diff_manifests_warns_for_low_overlap():
    old_manifest = _make_manifest_stub("old")
    new_manifest = _make_manifest_stub("new")

    _add_file(old_manifest, name="A/one.bin", chunk_ids=[0x100], bundle_id=0x6001)
    _add_file(old_manifest, name="A/two.bin", chunk_ids=[0x101], bundle_id=0x6002)
    _add_file(new_manifest, name="B/one.bin", chunk_ids=[0x200], bundle_id=0x7001)
    _add_file(new_manifest, name="B/two.bin", chunk_ids=[0x201], bundle_id=0x7002)

    report = diff_manifests(old_manifest, new_manifest)
    assert any("没有任何公共路径" in warning for warning in report.summary.warnings)


def test_diff_manifests_supports_filter_source_old():
    old_manifest = _make_manifest_stub("old")
    new_manifest = _make_manifest_stub("new")

    _add_file(
        old_manifest,
        name="DATA/lang_only.bin",
        chunk_ids=[0x901],
        flags=["zh_CN"],
        bundle_id=0xA001,
    )
    _add_file(
        new_manifest,
        name="DATA/lang_only.bin",
        chunk_ids=[0x902],
        flags=None,
        bundle_id=0xB001,
    )

    report_both = diff_manifests(old_manifest, new_manifest, flags="zh_CN", filter_source="both")
    assert report_both.summary.removed_count == 1
    assert report_both.summary.changed_count == 0

    report_old = diff_manifests(old_manifest, new_manifest, flags="zh_CN", filter_source="old")
    assert report_old.summary.removed_count == 0
    assert report_old.summary.changed_count == 1
    assert report_old.changed[0].path == "DATA/lang_only.bin"


def test_diff_manifests_supports_filter_source_new_with_unflagged_files():
    old_manifest = _make_manifest_stub("old")
    new_manifest = _make_manifest_stub("new")

    _add_file(
        old_manifest,
        name="DATA/lang.zh.bin",
        chunk_ids=[0xA101],
        flags=["zh_CN"],
        bundle_id=0xA111,
    )
    _add_file(
        new_manifest,
        name="DATA/lang.zh.bin",
        chunk_ids=[0xA202],
        flags=["zh_CN"],
        bundle_id=0xA222,
    )
    _add_file(old_manifest, name="DATA/plain.bin", chunk_ids=[0xA303], flags=None, bundle_id=0xA333)
    _add_file(new_manifest, name="DATA/plain.bin", chunk_ids=[0xA303], flags=None, bundle_id=0xA444)
    _add_file(new_manifest, name="DATA/new.zh.bin", chunk_ids=[0xA505], flags=["zh_CN"], bundle_id=0xA555)

    report = diff_manifests(
        old_manifest,
        new_manifest,
        flags=["zh_CN"],
        include_unflagged_when_flags=True,
        include_unchanged=True,
        filter_source="new",
    )
    assert {item.path for item in report.changed} == {"DATA/lang.zh.bin"}
    assert {item.path for item in report.added} == {"DATA/new.zh.bin"}
    assert {item.path for item in report.unchanged} == {"DATA/plain.bin"}


def test_diff_manifests_validates_arguments():
    old_manifest = _make_manifest_stub("old")
    new_manifest = _make_manifest_stub("new")

    with pytest.raises(TypeError, match="manifest 参数必须"):
        diff_manifests(123, new_manifest)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="filter_source"):
        diff_manifests(old_manifest, new_manifest, filter_source="invalid")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="hash_type_mismatch_mode"):
        diff_manifests(old_manifest, new_manifest, hash_type_mismatch_mode="invalid")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="overlap_warning_threshold"):
        diff_manifests(old_manifest, new_manifest, overlap_warning_threshold=1.5)


def test_diff_manifests_accepts_manifest_file_path(monkeypatch, tmp_path):
    old_file = tmp_path / "old.manifest"
    new_file = tmp_path / "new.manifest"
    old_file.write_bytes(b"OLD")
    new_file.write_bytes(b"NEW")

    def _fake_parse_rman(self, file_obj):  # pylint: disable=unused-argument
        marker = file_obj.read()
        self.bundles = []
        self.chunks = {}
        self.flags = {}
        self.files = {}
        if marker == b"OLD":
            _add_file(self, name="DATA/sample.bin", chunk_ids=[0x01], bundle_id=0xC001)
        elif marker == b"NEW":
            _add_file(self, name="DATA/sample.bin", chunk_ids=[0x02], bundle_id=0xD001)

    monkeypatch.setattr(PatcherManifest, "parse_rman", _fake_parse_rman)

    report = diff_manifests(str(old_file), new_file)
    assert report.summary.changed_count == 1
    assert report.changed[0].path == "DATA/sample.bin"


@dataclass
class _FakeSection:
    path_hash: int
    offset: int
    compressed_size: int
    size: int
    type: int
    duplicate: bool = False
    subchunk_count: int = 0
    sha256: int | None = None


class _FakeHeader:
    def __init__(self, sections: list[_FakeSection], path_map: dict[str, int]):
        self.files = sections
        self._path_map = {key.lower(): value for key, value in path_map.items()}

    def _get_hash_for_path(self, path: str) -> int:
        return self._path_map.get(path.lower(), 0xFFFFFFFFFFFFFFFF)


def test_diff_wad_headers_supports_focus_paths_and_target_wads(monkeypatch):
    old_manifest = _make_manifest_stub("old")
    new_manifest = _make_manifest_stub("new")

    changed_wad = "DATA/FINAL/Aatrox.zh_CN.wad.client"
    stable_wad = "DATA/FINAL/Stable.zh_CN.wad.client"
    removed_wad = "DATA/FINAL/OldOnly.zh_CN.wad.client"
    added_wad = "DATA/FINAL/NewOnly.zh_CN.wad.client"

    _add_file(old_manifest, name=changed_wad, chunk_ids=[0x1111], flags=["zh_CN"], bundle_id=0x8001)
    _add_file(new_manifest, name=changed_wad, chunk_ids=[0x2222], flags=["zh_CN"], bundle_id=0x9001)
    _add_file(old_manifest, name=stable_wad, chunk_ids=[0x3333], flags=["zh_CN"], bundle_id=0x8002)
    _add_file(new_manifest, name=stable_wad, chunk_ids=[0x3333], flags=["zh_CN"], bundle_id=0x9002)
    _add_file(old_manifest, name=removed_wad, chunk_ids=[0x4444], flags=["zh_CN"], bundle_id=0x8003)
    _add_file(new_manifest, name=added_wad, chunk_ids=[0x5555], flags=["zh_CN"], bundle_id=0x9003)

    headers = {
        ("old.manifest", changed_wad): _FakeHeader(
            sections=[
                _FakeSection(path_hash=0x1, offset=0, compressed_size=10, size=20, type=3),
                _FakeSection(path_hash=0x2, offset=10, compressed_size=8, size=16, type=3),
            ],
            path_map={"focus.bin": 0x1, "missing.bin": 0x999},
        ),
        ("new.manifest", changed_wad): _FakeHeader(
            sections=[
                _FakeSection(path_hash=0x1, offset=0, compressed_size=11, size=20, type=3),
                _FakeSection(path_hash=0x3, offset=11, compressed_size=9, size=18, type=3),
            ],
            path_map={"focus.bin": 0x1, "missing.bin": 0x999},
        ),
        ("old.manifest", stable_wad): _FakeHeader(
            sections=[_FakeSection(path_hash=0xA, offset=0, compressed_size=4, size=4, type=0)],
            path_map={},
        ),
        ("new.manifest", stable_wad): _FakeHeader(
            sections=[_FakeSection(path_hash=0xA, offset=0, compressed_size=4, size=4, type=0)],
            path_map={},
        ),
    }

    def _fake_get_wad_header(self: WADExtractor, wad_file: PatcherFile):
        return headers[(self.manifest.file, wad_file.name)]

    monkeypatch.setattr(WADExtractor, "get_wad_header", _fake_get_wad_header)

    report = diff_wad_headers(
        old_manifest,
        new_manifest,
        flags="zh_CN",
        target_wad_files=[changed_wad, stable_wad, removed_wad, added_wad],
        inner_paths={changed_wad: ["focus.bin", "missing.bin"]},
        include_unchanged=True,
    )

    entries = {item.wad_path: item for item in report.files}
    assert entries[added_wad].status == "added"
    assert entries[removed_wad].status == "removed"
    assert entries[stable_wad].status == "unchanged"

    changed_entry = entries[changed_wad]
    assert changed_entry.status == "changed"
    assert changed_entry.missing_focused_paths == ("missing.bin",)
    assert [item.path_hash for item in changed_entry.section_diffs] == [0x1]
    assert changed_entry.section_diffs[0].status == "changed"


def test_diff_wad_headers_requires_target_wad_files():
    old_manifest = _make_manifest_stub("old")
    new_manifest = _make_manifest_stub("new")
    with pytest.raises(ValueError, match="target_wad_files"):
        diff_wad_headers(old_manifest, new_manifest)


def test_diff_wad_headers_rejects_non_wad_targets():
    old_manifest = _make_manifest_stub("old")
    new_manifest = _make_manifest_stub("new")
    with pytest.raises(ValueError, match="\\.wad\\.client"):
        diff_wad_headers(
            old_manifest,
            new_manifest,
            target_wad_files=["DATA/FINAL/not_wad.txt"],
        )


def test_diff_wad_headers_validates_manifest_inputs():
    old_manifest = _make_manifest_stub("old")
    wad_path = "DATA/FINAL/Test.zh_CN.wad.client"

    with pytest.raises(ValueError, match="需同时提供"):
        diff_wad_headers(old_manifest=old_manifest, target_wad_files=[wad_path])

    with pytest.raises(ValueError, match="必须提供 old_manifest/new_manifest"):
        diff_wad_headers(target_wad_files=[wad_path])


def test_diff_wad_headers_excludes_unchanged_by_default(monkeypatch):
    old_manifest = _make_manifest_stub("old")
    new_manifest = _make_manifest_stub("new")

    changed_wad = "DATA/FINAL/Akshan.zh_CN.wad.client"
    stable_wad = "DATA/FINAL/Renata.zh_CN.wad.client"

    _add_file(old_manifest, name=changed_wad, chunk_ids=[0xC101], flags=["zh_CN"], bundle_id=0xD001)
    _add_file(new_manifest, name=changed_wad, chunk_ids=[0xC202], flags=["zh_CN"], bundle_id=0xE001)
    _add_file(old_manifest, name=stable_wad, chunk_ids=[0xC303], flags=["zh_CN"], bundle_id=0xD002)
    _add_file(new_manifest, name=stable_wad, chunk_ids=[0xC303], flags=["zh_CN"], bundle_id=0xE002)

    headers = {
        ("old.manifest", changed_wad): _FakeHeader(
            sections=[
                _FakeSection(path_hash=0x11, offset=0, compressed_size=10, size=20, type=3),
                _FakeSection(path_hash=0x12, offset=20, compressed_size=6, size=6, type=0),
            ],
            path_map={},
        ),
        ("new.manifest", changed_wad): _FakeHeader(
            sections=[
                _FakeSection(path_hash=0x11, offset=0, compressed_size=12, size=20, type=3),
                _FakeSection(path_hash=0x12, offset=20, compressed_size=6, size=6, type=0),
            ],
            path_map={},
        ),
        ("old.manifest", stable_wad): _FakeHeader(
            sections=[_FakeSection(path_hash=0x21, offset=0, compressed_size=6, size=6, type=0)],
            path_map={},
        ),
        ("new.manifest", stable_wad): _FakeHeader(
            sections=[_FakeSection(path_hash=0x21, offset=0, compressed_size=6, size=6, type=0)],
            path_map={},
        ),
    }

    def _fake_get_wad_header(self: WADExtractor, wad_file: PatcherFile):
        return headers[(self.manifest.file, wad_file.name)]

    monkeypatch.setattr(WADExtractor, "get_wad_header", _fake_get_wad_header)

    report = diff_wad_headers(
        old_manifest,
        new_manifest,
        flags="zh_CN",
        target_wad_files=[changed_wad, stable_wad],
    )
    assert {item.wad_path for item in report.files} == {changed_wad}
    assert report.summary.changed_count == 1
    assert report.summary.unchanged_count == 0
    assert all(item.status != "unchanged" for item in report.files[0].section_diffs)
    assert [item.path_hash for item in report.files[0].section_diffs] == [0x11]


def test_diff_wad_headers_marks_error_when_header_read_fails(monkeypatch):
    old_manifest = _make_manifest_stub("old")
    new_manifest = _make_manifest_stub("new")
    wad_path = "DATA/FINAL/ErrorCase.zh_CN.wad.client"

    _add_file(old_manifest, name=wad_path, chunk_ids=[0xED01], flags=["zh_CN"], bundle_id=0xED11)
    _add_file(new_manifest, name=wad_path, chunk_ids=[0xED02], flags=["zh_CN"], bundle_id=0xED22)

    def _raise_get_wad_header(self: WADExtractor, wad_file: PatcherFile):  # noqa: ARG001
        raise RuntimeError("mock get_wad_header failed")

    monkeypatch.setattr(WADExtractor, "get_wad_header", _raise_get_wad_header)

    report = diff_wad_headers(
        old_manifest,
        new_manifest,
        target_wad_files=[wad_path],
        include_unchanged=True,
    )
    assert report.summary.error_count == 1
    assert report.files[0].status == "error"
    assert "mock get_wad_header failed" in (report.files[0].warning or "")


def test_diff_wad_headers_can_reuse_manifest_report_context(monkeypatch):
    old_manifest = _make_manifest_stub("old")
    new_manifest = _make_manifest_stub("new")
    wad_path = "DATA/FINAL/Akshan.zh_CN.wad.client"

    _add_file(old_manifest, name=wad_path, chunk_ids=[0xDE01], flags=["zh_CN"], bundle_id=0xE101)
    _add_file(new_manifest, name=wad_path, chunk_ids=[0xDE02], flags=["zh_CN"], bundle_id=0xE201)

    headers = {
        ("old.manifest", wad_path): _FakeHeader(
            sections=[_FakeSection(path_hash=0x31, offset=0, compressed_size=4, size=8, type=3)],
            path_map={},
        ),
        ("new.manifest", wad_path): _FakeHeader(
            sections=[_FakeSection(path_hash=0x31, offset=0, compressed_size=5, size=8, type=3)],
            path_map={},
        ),
    }

    def _fake_get_wad_header(self: WADExtractor, wad_file: PatcherFile):
        return headers[(self.manifest.file, wad_file.name)]

    monkeypatch.setattr(WADExtractor, "get_wad_header", _fake_get_wad_header)

    manifest_report = diff_manifests(
        old_manifest,
        new_manifest,
        target_files=[wad_path],
        include_unchanged=True,
        detect_moves=False,
    )
    report = diff_wad_headers(
        manifest_report=manifest_report,
        target_wad_files=[wad_path],
        include_unchanged=True,
    )
    assert report.summary.changed_count == 1
    assert report.files[0].wad_path == wad_path

    object.__setattr__(manifest_report, "_old_manifest_obj", None)
    object.__setattr__(manifest_report, "_new_manifest_obj", None)
    with pytest.raises(ValueError, match="没有可复用的 Manifest 上下文"):
        diff_wad_headers(
            manifest_report=manifest_report,
            target_wad_files=[wad_path],
            include_unchanged=True,
        )


def test_diff_wad_headers_reports_added_and_removed_sections(monkeypatch):
    old_manifest = _make_manifest_stub("old")
    new_manifest = _make_manifest_stub("new")
    wad_path = "DATA/FINAL/SectionDelta.zh_CN.wad.client"

    _add_file(old_manifest, name=wad_path, chunk_ids=[0xAA01], flags=["zh_CN"], bundle_id=0xAB01)
    _add_file(new_manifest, name=wad_path, chunk_ids=[0xAA02], flags=["zh_CN"], bundle_id=0xAB02)

    headers = {
        ("old.manifest", wad_path): _FakeHeader(
            sections=[_FakeSection(path_hash=0x100, offset=0, compressed_size=8, size=16, type=3)],
            path_map={},
        ),
        ("new.manifest", wad_path): _FakeHeader(
            sections=[_FakeSection(path_hash=0x200, offset=0, compressed_size=8, size=16, type=3)],
            path_map={},
        ),
    }

    def _fake_get_wad_header(self: WADExtractor, wad_file: PatcherFile):
        return headers[(self.manifest.file, wad_file.name)]

    monkeypatch.setattr(WADExtractor, "get_wad_header", _fake_get_wad_header)

    report = diff_wad_headers(
        old_manifest,
        new_manifest,
        target_wad_files=[wad_path],
        include_unchanged=True,
    )
    statuses = {item.status for item in report.files[0].section_diffs}
    assert statuses == {"added", "removed"}


def test_diff_wad_headers_supports_fallback_path_hash_function(monkeypatch):
    old_manifest = _make_manifest_stub("old")
    new_manifest = _make_manifest_stub("new")
    wad_path = "DATA/FINAL/FallbackHash.zh_CN.wad.client"

    _add_file(old_manifest, name=wad_path, chunk_ids=[0xB001], flags=["zh_CN"], bundle_id=0xB101)
    _add_file(new_manifest, name=wad_path, chunk_ids=[0xB001], flags=["zh_CN"], bundle_id=0xB202)

    class _FallbackHashHeader:
        def __init__(self, sections: list[_FakeSection]):
            self.files = sections

        @staticmethod
        def get_hash(path: str) -> int:
            return 0xABC if path.lower() == "focus.bin" else 0xFFFFFFFFFFFFFFFF

    headers = {
        ("old.manifest", wad_path): _FallbackHashHeader(
            sections=[_FakeSection(path_hash=0xABC, offset=0, compressed_size=4, size=4, type=0)]
        ),
        ("new.manifest", wad_path): _FallbackHashHeader(
            sections=[_FakeSection(path_hash=0xABC, offset=0, compressed_size=4, size=4, type=0)]
        ),
    }

    def _fake_get_wad_header(self: WADExtractor, wad_file: PatcherFile):
        return headers[(self.manifest.file, wad_file.name)]

    monkeypatch.setattr(WADExtractor, "get_wad_header", _fake_get_wad_header)

    report = diff_wad_headers(
        old_manifest,
        new_manifest,
        target_wad_files=[wad_path],
        inner_paths=["focus.bin"],
        include_unchanged=True,
    )
    assert report.files[0].section_diffs[0].path_hash == 0xABC


def test_diff_wad_headers_raises_when_header_lacks_hash_function(monkeypatch):
    old_manifest = _make_manifest_stub("old")
    new_manifest = _make_manifest_stub("new")
    wad_path = "DATA/FINAL/NoHash.zh_CN.wad.client"

    _add_file(old_manifest, name=wad_path, chunk_ids=[0xC001], flags=["zh_CN"], bundle_id=0xC101)
    _add_file(new_manifest, name=wad_path, chunk_ids=[0xC002], flags=["zh_CN"], bundle_id=0xC202)

    class _NoHashHeader:
        def __init__(self, sections: list[_FakeSection]):
            self.files = sections

    headers = {
        ("old.manifest", wad_path): _NoHashHeader(
            sections=[_FakeSection(path_hash=0x1, offset=0, compressed_size=3, size=3, type=0)]
        ),
        ("new.manifest", wad_path): _NoHashHeader(
            sections=[_FakeSection(path_hash=0x2, offset=0, compressed_size=3, size=3, type=0)]
        ),
    }

    def _fake_get_wad_header(self: WADExtractor, wad_file: PatcherFile):
        return headers[(self.manifest.file, wad_file.name)]

    monkeypatch.setattr(WADExtractor, "get_wad_header", _fake_get_wad_header)

    with pytest.raises(ValueError, match="不支持路径哈希计算"):
        diff_wad_headers(
            old_manifest,
            new_manifest,
            target_wad_files=[wad_path],
            inner_paths=["focus.bin"],
            include_unchanged=True,
        )


def test_wad_header_diff_report_json_helpers(monkeypatch, tmp_path):
    old_manifest = _make_manifest_stub("old")
    new_manifest = _make_manifest_stub("new")
    wad_path = "DATA/FINAL/Akshan.zh_CN.wad.client"

    _add_file(old_manifest, name=wad_path, chunk_ids=[0xB101], flags=["zh_CN"], bundle_id=0xB001)
    _add_file(new_manifest, name=wad_path, chunk_ids=[0xB202], flags=["zh_CN"], bundle_id=0xC001)

    headers = {
        ("old.manifest", wad_path): _FakeHeader(
            sections=[_FakeSection(path_hash=0x1, offset=0, compressed_size=5, size=10, type=3)],
            path_map={"focus.bin": 0x1},
        ),
        ("new.manifest", wad_path): _FakeHeader(
            sections=[_FakeSection(path_hash=0x1, offset=0, compressed_size=7, size=10, type=3)],
            path_map={"focus.bin": 0x1},
        ),
    }

    def _fake_get_wad_header(self: WADExtractor, wad_file: PatcherFile):
        return headers[(self.manifest.file, wad_file.name)]

    monkeypatch.setattr(WADExtractor, "get_wad_header", _fake_get_wad_header)

    report = diff_wad_headers(
        old_manifest,
        new_manifest,
        target_wad_files=[wad_path],
        flags="zh_CN",
        include_unchanged=True,
    )

    manifest_changed_entry = report.manifest_report.changed[0]
    assert manifest_changed_entry.path == wad_path
    assert manifest_changed_entry.section_diffs is not None
    assert manifest_changed_entry.section_diffs[0].path_hash == 0x1

    payload = report.to_dict()
    assert payload["summary"]["changed_count"] == 1
    payload_manifest_entry = payload["manifest_report"]["changed"][0]
    assert payload_manifest_entry["section_diffs"][0]["path_hash"] == 0x1

    compact_payload = report.to_dict(collapse_manifest_equal_pairs=True)
    manifest_entry = compact_payload["manifest_report"]["changed"][0]
    assert "old_size" not in manifest_entry
    assert "size" in manifest_entry

    summary_payload = report.to_dict(manifest_report_mode="summary")
    assert "manifest_report" in summary_payload
    assert set(summary_payload["manifest_report"]) == {"summary", "moved"}

    without_manifest_payload = report.to_dict(manifest_report_mode="none")
    assert "manifest_report" not in without_manifest_payload
    assert without_manifest_payload["summary"]["changed_count"] == 1

    text = report.to_pretty_json()
    parsed = json.loads(text)
    assert parsed["files"][0]["wad_path"] == wad_path

    out_file = tmp_path / "wad_header_diff.json"
    saved = report.dump_pretty_json(out_file, collapse_manifest_equal_pairs=True)
    assert saved == str(out_file)
    assert out_file.is_file()
    saved_payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert saved_payload["summary"]["changed_count"] == 1



def test_diff_manifests_handles_hash_type_mismatch_modes():
    old_manifest = _make_manifest_stub("old")
    new_manifest = _make_manifest_stub("new")

    old_file = _add_file(old_manifest, name="DATA/mismatch.bin", chunk_ids=[0x1001], size=10, bundle_id=0xE001)
    new_file = _add_file(new_manifest, name="DATA/mismatch.bin", chunk_ids=[0x2002], size=10, bundle_id=0xF001)
    old_file.chunk_hash_types = {0x1001: 3}
    new_file.chunk_hash_types = {0x2002: 0}

    loose_report = diff_manifests(old_manifest, new_manifest, hash_type_mismatch_mode="loose", include_unchanged=True)
    assert loose_report.summary.changed_count == 0
    assert loose_report.summary.unchanged_count == 1
    assert any("hash_type_mismatch_mode='strict'" in warning for warning in loose_report.summary.warnings)

    strict_report = diff_manifests(old_manifest, new_manifest, hash_type_mismatch_mode="strict")
    assert strict_report.summary.changed_count == 1
    assert "chunk_hash_types" in strict_report.changed[0].changed_fields


def test_manifest_diff_report_json_helpers(tmp_path):
    old_manifest = _make_manifest_stub("old")
    new_manifest = _make_manifest_stub("new")

    _add_file(old_manifest, name="DATA/sample.bin", chunk_ids=[0x11], bundle_id=0xAA01)
    _add_file(new_manifest, name="DATA/sample.bin", chunk_ids=[0x11], bundle_id=0xBB01)

    report = diff_manifests(old_manifest, new_manifest, include_unchanged=True)
    payload = report.to_dict()
    assert payload["summary"]["unchanged_count"] == 1

    compact_payload = report.to_dict(collapse_equal_pairs=True)
    compact_entry = compact_payload["unchanged"][0]
    assert "old_size" not in compact_entry
    assert "new_size" not in compact_entry
    assert compact_entry["size"] == 1
    assert compact_entry["flags"] is None
    assert compact_entry["link"] == ""
    assert "chunk_digest" in compact_entry

    text = report.to_pretty_json()
    parsed = json.loads(text)
    assert parsed["summary"]["unchanged_count"] == 1

    compact_text = report.to_pretty_json(collapse_equal_pairs=True)
    compact_parsed = json.loads(compact_text)
    assert "old_size" not in compact_parsed["unchanged"][0]
    assert "size" in compact_parsed["unchanged"][0]

    out_file = tmp_path / "diff.json"
    saved = report.dump_pretty_json(out_file, collapse_equal_pairs=True)
    assert saved == str(out_file)
    assert out_file.is_file()
    saved_payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert "old_flags" not in saved_payload["unchanged"][0]
    assert "flags" in saved_payload["unchanged"][0]
