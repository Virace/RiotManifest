"""`LeagueManifestInspector` 的定向测试."""

from __future__ import annotations

from pathlib import Path

import pytest

from riotmanifest.game import (
    ConsistentGameManifestNotFoundError,
    LeagueManifestInspector,
    ManifestInspectionError,
    VersionDisplayMode,
    VersionInfo,
    VersionMatchMode,
)
from riotmanifest.game.factory import ManifestRef


class _DummyManifest:
    """最小化 manifest 测试替身."""

    def __init__(self, file: str, path: str) -> None:
        self.file = file
        self.path = path
        self.files = dict(_DummyManifest.registry[file])


_DummyManifest.registry: dict[str, dict[str, object]] = {}


def _set_manifest_registry(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, dict[str, object]]) -> None:
    _DummyManifest.registry = mapping
    monkeypatch.setattr("riotmanifest.game.inspection.PatcherManifest", _DummyManifest)


def _make_version(
    *,
    normalized_build: str,
    patch_version: str,
    metadata_version: str | None = None,
    exe_version: str | None = None,
) -> VersionInfo:
    return VersionInfo(
        normalized_build=normalized_build,
        patch_version=patch_version,
        metadata_version=metadata_version,
        exe_version=exe_version,
    )


def _make_manifest_ref(
    *,
    artifact_group: str,
    url: str,
    version: VersionInfo | None,
) -> ManifestRef:
    return ManifestRef(
        artifact_group=artifact_group,
        region="inspection",
        source="manifest_inspector",
        url=url,
        manifest_id=Path(url).name.removesuffix(".manifest"),
        version=version,
    )


def test_inspect_manifest_prefers_content_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_manifest_registry(
        monkeypatch,
        {
            "https://example.invalid/game.manifest": {
                "content-metadata.json": object(),
                "League of Legends.exe": object(),
            }
        },
    )
    monkeypatch.setattr(
        "riotmanifest.game.inspection._download_manifest_payload",
        lambda **kwargs: b'{"version": "16.5.7511533+branch.releases-16-5.content.release"}',
    )

    manifest = LeagueManifestInspector().inspect_manifest("https://example.invalid/game.manifest")

    assert manifest.artifact_group == "game"
    assert manifest.version is not None
    assert manifest.version.normalized_build == "16.5.7511533"
    assert manifest.version.patch_version == "16.5"


def test_inspect_manifest_game_falls_back_to_exe(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_manifest_registry(
        monkeypatch,
        {"https://example.invalid/game.manifest": {"League of Legends.exe": object()}},
    )
    monkeypatch.setattr(
        "riotmanifest.game.inspection._download_manifest_payload",
        lambda **kwargs: b"fake_pe_payload",
    )
    monkeypatch.setattr(
        "riotmanifest.game.inspection._LcuVersionResolver._extract_windows_version",
        lambda self, payload: "16.5.751.1533",
    )

    manifest = LeagueManifestInspector().inspect_manifest("https://example.invalid/game.manifest")

    assert manifest.artifact_group == "game"
    assert manifest.version is not None
    assert manifest.version.normalized_build == "16.5.7511533"
    assert manifest.version.dotted_version == "16.5.751.1533"


def test_inspect_manifest_detects_lcu(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_manifest_registry(
        monkeypatch,
        {"https://example.invalid/lcu.manifest": {"LeagueClient.exe": object()}},
    )
    monkeypatch.setattr(
        "riotmanifest.game.inspection._download_manifest_payload",
        lambda **kwargs: b"fake_pe_payload",
    )
    monkeypatch.setattr(
        "riotmanifest.game.inspection._LcuVersionResolver._extract_windows_version",
        lambda self, payload: "16.5.751.8496",
    )

    manifest = LeagueManifestInspector().inspect_manifest("https://example.invalid/lcu.manifest")

    assert manifest.artifact_group == "lcu"
    assert manifest.version is not None
    assert manifest.version.normalized_build == "16.5.7518496"
    assert manifest.version.patch_version == "16.5"


def test_inspect_manifest_returns_unknown_for_non_lol_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_manifest_registry(monkeypatch, {"/tmp/other.manifest": {"foo.txt": object()}})

    manifest = LeagueManifestInspector().inspect_manifest("/tmp/other.manifest")

    assert manifest.artifact_group == "unknown"
    assert manifest.version is None


def test_inspect_pair_matches_ignore_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    inspector = LeagueManifestInspector()
    monkeypatch.setattr(
        inspector,
        "inspect_manifest",
        lambda source: {
            "game": _make_manifest_ref(
                artifact_group="game",
                url="https://example.invalid/game.manifest",
                version=_make_version(
                    normalized_build="16.5.7511533",
                    patch_version="16.5",
                    metadata_version="16.5.7511533",
                ),
            ),
            "lcu": _make_manifest_ref(
                artifact_group="lcu",
                url="https://example.invalid/lcu.manifest",
                version=_make_version(
                    normalized_build="16.5.7518496",
                    patch_version="16.5",
                    exe_version="16.5.751.8496",
                ),
            ),
        }[source],
    )

    pair = inspector.inspect_pair(
        "game",
        "lcu",
        match_mode=VersionMatchMode.IGNORE_REVISION,
    )

    assert pair.match_reason == "ignore_revision_fallback"
    assert pair.is_exact_match is False
    assert pair.candidate_count == 1
    assert str(pair.version) == "16.5"


def test_inspect_pair_supports_patch_latest_for_newer_game(monkeypatch: pytest.MonkeyPatch) -> None:
    inspector = LeagueManifestInspector()
    monkeypatch.setattr(
        inspector,
        "inspect_manifest",
        lambda source: {
            "game": _make_manifest_ref(
                artifact_group="game",
                url="https://example.invalid/game.manifest",
                version=_make_version(
                    normalized_build="16.5.7519084",
                    patch_version="16.5",
                    metadata_version="16.5.7519084",
                ),
            ),
            "lcu": _make_manifest_ref(
                artifact_group="lcu",
                url="https://example.invalid/lcu.manifest",
                version=_make_version(
                    normalized_build="16.5.7518496",
                    patch_version="16.5",
                    exe_version="16.5.751.8496",
                ),
            ),
        }[source],
    )

    pair = inspector.inspect_pair(
        "lcu",
        "game",
        match_mode=VersionMatchMode.PATCH_LATEST,
        version_display_mode=VersionDisplayMode.GAME,
    )

    assert pair.match_reason == "patch_latest_fallback"
    assert pair.version.value == "16.5.7519084"


def test_inspect_pair_strict_raises_without_exact_match(monkeypatch: pytest.MonkeyPatch) -> None:
    inspector = LeagueManifestInspector()
    monkeypatch.setattr(
        inspector,
        "inspect_manifest",
        lambda source: {
            "game": _make_manifest_ref(
                artifact_group="game",
                url="https://example.invalid/game.manifest",
                version=_make_version(
                    normalized_build="16.5.7511533",
                    patch_version="16.5",
                    metadata_version="16.5.7511533",
                ),
            ),
            "lcu": _make_manifest_ref(
                artifact_group="lcu",
                url="https://example.invalid/lcu.manifest",
                version=_make_version(
                    normalized_build="16.5.7518496",
                    patch_version="16.5",
                    exe_version="16.5.751.8496",
                ),
            ),
        }[source],
    )

    with pytest.raises(ConsistentGameManifestNotFoundError, match="严格匹配"):
        inspector.inspect_pair("lcu", "game", match_mode=VersionMatchMode.STRICT)


def test_inspect_pair_rejects_non_pair_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    inspector = LeagueManifestInspector()
    monkeypatch.setattr(
        inspector,
        "inspect_manifest",
        lambda source: _make_manifest_ref(
            artifact_group="game",
            url=f"https://example.invalid/{source}.manifest",
            version=_make_version(
                normalized_build="16.5.7511533",
                patch_version="16.5",
                metadata_version="16.5.7511533",
            ),
        ),
    )

    with pytest.raises(ManifestInspectionError, match="无法组成一对"):
        inspector.inspect_pair("a", "b")


def test_inspect_manifests_dispatches_by_count(monkeypatch: pytest.MonkeyPatch) -> None:
    inspector = LeagueManifestInspector()
    single_result = _make_manifest_ref(
        artifact_group="unknown",
        url="/tmp/one.manifest",
        version=None,
    )
    pair_result = object()
    monkeypatch.setattr(inspector, "inspect_manifest", lambda source: single_result)
    monkeypatch.setattr(inspector, "inspect_pair", lambda *args, **kwargs: pair_result)

    assert inspector.inspect_manifests("one") is single_result
    assert inspector.inspect_manifests("one", "two") is pair_result
    with pytest.raises(ValueError, match="只支持 1 个或 2 个"):
        inspector.inspect_manifests("one", "two", "three")
