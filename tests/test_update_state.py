"""清单存档与 installed.json 状态管理单测."""

import json
import os
from pathlib import Path

import pytest

from riotmanifest.manifest import PatcherManifest
from riotmanifest.update.state import LEGACY_STATE_SCHEMA, STATE_SCHEMA, ManifestArchive


def test_save_then_load_roundtrip(tmp_path: Path):
    archive = ManifestArchive(tmp_path)

    archive.save(
        0x037EC59D5BD7C5D3,
        b"raw-manifest-bytes",
        "https://example.invalid/a.manifest",
        ["DATA\\b.bin", "Config/a.json", "DATA/b.bin"],
    )

    state = archive.load_installed()
    assert state is not None
    assert state.schema == STATE_SCHEMA
    assert state.manifest_id == "037EC59D5BD7C5D3"
    assert state.manifest_file == "manifests/037EC59D5BD7C5D3.manifest"
    assert state.source == "https://example.invalid/a.manifest"
    assert state.updated_at
    assert state.files == ["Config/a.json", "DATA/b.bin"]

    manifest_path = archive.installed_manifest_path()
    assert manifest_path is not None
    assert manifest_path.read_bytes() == b"raw-manifest-bytes"


def test_load_missing_or_corrupt_returns_none(tmp_path: Path):
    archive = ManifestArchive(tmp_path)
    assert archive.load_installed() is None
    assert archive.installed_manifest_path() is None

    archive.installed_file.parent.mkdir(parents=True, exist_ok=True)
    archive.installed_file.write_text("{broken json", "utf-8")
    assert archive.load_installed() is None


def test_load_unknown_schema_returns_none(tmp_path: Path):
    archive = ManifestArchive(tmp_path)
    archive.installed_file.parent.mkdir(parents=True, exist_ok=True)
    archive.installed_file.write_text(
        json.dumps({"schema": 99, "manifest_id": "AA", "manifest_file": "x"}),
        "utf-8",
    )
    assert archive.load_installed() is None


def test_load_legacy_schema_keeps_manifest_pointer_without_coverage(tmp_path: Path):
    archive = ManifestArchive(tmp_path)
    archive.installed_file.parent.mkdir(parents=True, exist_ok=True)
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
    manifest_path = archive.root / "manifests/0000000000000001.manifest"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_bytes(b"legacy")

    state = archive.load_installed()

    assert state is not None
    assert state.schema == LEGACY_STATE_SCHEMA
    assert state.files == []
    assert archive.installed_manifest_path() == manifest_path


def test_load_schema_two_with_invalid_files_returns_none(tmp_path: Path):
    archive = ManifestArchive(tmp_path)
    archive.installed_file.parent.mkdir(parents=True, exist_ok=True)
    archive.installed_file.write_text(
        json.dumps(
            {
                "schema": STATE_SCHEMA,
                "manifest_id": "0000000000000001",
                "manifest_file": "manifests/0000000000000001.manifest",
                "files": ["../outside.bin"],
            }
        ),
        "utf-8",
    )

    assert archive.load_installed() is None


@pytest.mark.parametrize(
    "path",
    [
        "/absolute.bin",
        "trailing/",
        "double//segment.bin",
        "./dot.bin",
        "parent/../escape.bin",
        "bad:segment/file.bin",
        "dir/bad:segment.bin",
    ],
)
def test_state_paths_reject_malformed_segments(tmp_path: Path, path: str):
    archive = ManifestArchive(tmp_path)

    with pytest.raises(ValueError, match="manifest 相对路径"):
        archive.save(0x1, b"v1", "src", [path])


@pytest.mark.parametrize("manifest_file", ["/absolute.manifest", "manifests//bad.manifest", "bad:id.manifest"])
def test_load_rejects_invalid_manifest_file(tmp_path: Path, manifest_file: str):
    archive = ManifestArchive(tmp_path)
    archive.installed_file.parent.mkdir(parents=True, exist_ok=True)
    archive.installed_file.write_text(
        json.dumps(
            {
                "schema": STATE_SCHEMA,
                "manifest_id": "0000000000000001",
                "manifest_file": manifest_file,
                "files": [],
            }
        ),
        "utf-8",
    )

    assert archive.load_installed() is None


@pytest.mark.parametrize("files", [None, pytest.param("missing", id="missing-key")])
def test_load_schema_two_requires_files_array(tmp_path: Path, files):
    archive = ManifestArchive(tmp_path)
    archive.installed_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": STATE_SCHEMA,
        "manifest_id": "0000000000000001",
        "manifest_file": "manifests/0000000000000001.manifest",
    }
    if files != "missing":
        payload["files"] = files
    archive.installed_file.write_text(json.dumps(payload), "utf-8")

    assert archive.load_installed() is None


def test_save_rejects_string_instead_of_treating_it_as_path_list(tmp_path: Path):
    archive = ManifestArchive(tmp_path)

    with pytest.raises(ValueError, match="manifest 相对路径"):
        archive.save(0x1, b"v1", "src", "a.bin")


def test_save_keeps_only_current_and_previous(tmp_path: Path):
    archive = ManifestArchive(tmp_path)

    archive.save(0x1, b"v1", "src1")
    archive.save(0x2, b"v2", "src2")
    archive.save(0x3, b"v3", "src3")

    names = sorted(p.name for p in (archive.root / "manifests").glob("*.manifest"))
    assert names == ["0000000000000002.manifest", "0000000000000003.manifest"]
    state = archive.load_installed()
    assert state is not None
    assert state.manifest_id == "0000000000000003"


def test_save_atomicity_keeps_old_state_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    archive = ManifestArchive(tmp_path)
    archive.save(0x1, b"v1", "src1")
    before = archive.installed_file.read_text("utf-8")

    real_replace = os.replace

    def broken_replace(src, dst):
        raise OSError("mock replace failure")

    monkeypatch.setattr("riotmanifest.update.state.os.replace", broken_replace)
    with pytest.raises(OSError):
        archive.save(0x2, b"v2", "src2")
    monkeypatch.setattr("riotmanifest.update.state.os.replace", real_replace)

    # 替换失败时 installed.json 保持旧内容，状态指针不损坏。
    assert archive.installed_file.read_text("utf-8") == before
    state = archive.load_installed()
    assert state is not None
    assert state.manifest_id == "0000000000000001"


def test_patcher_manifest_retains_raw_bytes_and_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """__init__ 两条加载分支需保留原始字节；manifest_id 由 parse_rman 记录."""
    raw = b"fake-manifest-bytes"
    manifest_file = tmp_path / "a.manifest"
    manifest_file.write_bytes(raw)

    def fake_parse(self, stream):
        self.manifest_id = 0xABCD
        return None

    monkeypatch.setattr(PatcherManifest, "parse_rman", fake_parse)
    manifest = PatcherManifest(str(manifest_file), path=str(tmp_path))

    assert manifest.raw_bytes == raw
    assert manifest.manifest_id == 0xABCD
