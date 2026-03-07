import re
import types
from pathlib import Path

import pytest

from riotmanifest.extractor import WADExtractor
from riotmanifest.game import (
    ConsistentGameManifestNotFoundError,
    LeagueManifestResolver,
    LiveConfigNotFoundError,
    VersionDisplayMode,
    VersionInfo,
    VersionMatchMode,
)
from riotmanifest.manifest import PatcherBundle, PatcherFile, PatcherManifest


def _make_manifest_stub() -> PatcherManifest:
    manifest = object.__new__(PatcherManifest)
    manifest.file = "stub.manifest"
    manifest.path = ""
    manifest.bundle_url = "https://example.invalid/bundles/"
    manifest.files = {}

    def _filter_files(self, pattern=None, flag=None):  # pylint: disable=unused-argument
        if not pattern:
            return self.files.values()
        regex = re.compile(pattern, re.I)
        return filter(lambda file: bool(regex.search(file.name)), self.files.values())

    def _validate_chunk_hash(self, chunk_data, chunk_id, hash_type):  # pylint: disable=unused-argument
        return None

    manifest.filter_files = types.MethodType(_filter_files, manifest)
    manifest.validate_chunk_hash = types.MethodType(_validate_chunk_hash, manifest)
    return manifest


def _make_wad_file(manifest: PatcherManifest, name: str = "DATA/FINAL/Test.wad.client") -> PatcherFile:
    bundle = PatcherBundle(0x1001)
    bundle.add_chunk(chunk_id=0x2001, size=5, target_size=5)
    bundle.add_chunk(chunk_id=0x2002, size=5, target_size=5)
    wad_file = PatcherFile(
        name=name,
        size=10,
        link="",
        flags=None,
        chunks=bundle.chunks,
        manifest=manifest,
        chunk_hash_types={},
    )
    manifest.files[wad_file.name] = wad_file
    return wad_file


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


def test_extractor_instances_are_independent():
    manifest = _make_manifest_stub()
    first = WADExtractor(manifest)
    second = WADExtractor(manifest)
    assert first is not second


def test_chunk_cache_lru_eviction():
    manifest = _make_manifest_stub()
    extractor = WADExtractor(manifest, cache_max_entries=1, cache_max_bytes=16)

    extractor._cache_put((1, 1), b"first")
    extractor._cache_put((1, 2), b"second")

    assert extractor._cache_get((1, 1)) is None
    assert extractor._cache_get((1, 2)) == b"second"
    assert extractor.cache_stats()["entries"] == 1


def test_read_wad_range_cross_chunk(monkeypatch):
    manifest = _make_manifest_stub()
    wad_file = _make_wad_file(manifest)
    extractor = WADExtractor(manifest)
    payloads = {0x2001: b"ABCDE", 0x2002: b"FGHIJ"}

    def _fake_download(self, _wad_file, chunk):
        return payloads[chunk.chunk_id]

    monkeypatch.setattr(WADExtractor, "_download_chunk_bytes", _fake_download)
    assert extractor._read_wad_file_range(wad_file, start=3, length=4) == b"DEFG"


def test_extract_files_uses_wad_version_hash(monkeypatch):
    manifest = _make_manifest_stub()
    wad_file = _make_wad_file(manifest)
    extractor = WADExtractor(manifest)

    class _Section:
        path_hash = 0xAABBCCDD
        offset = 1
        compressed_size = 4

    class _FakeHeader:
        def __init__(self):
            self.files = [_Section()]

        @staticmethod
        def _get_hash_for_path(path: str) -> int:
            if path == "data/champions/test.bin":
                return 0xAABBCCDD
            return 0xFFFF

        @staticmethod
        def extract_by_section(section, file_path, raw=False, data=None):  # pylint: disable=unused-argument
            return data

    monkeypatch.setattr(WADExtractor, "get_wad_header", lambda self, _wad_file: _FakeHeader())
    monkeypatch.setattr(
        WADExtractor,
        "_read_wad_file_range",
        lambda self, wad_file, start, length: b"DATA",  # pylint: disable=unused-argument
    )

    results = extractor.extract_files({wad_file.name: ["data/champions/test.bin", "data/champions/missing.bin"]})
    assert results[wad_file.name]["data/champions/test.bin"] == b"DATA"
    assert results[wad_file.name]["data/champions/missing.bin"] is None


def test_extract_files_to_disk(tmp_path: Path, monkeypatch):
    manifest = _make_manifest_stub()
    wad_file = _make_wad_file(manifest)
    extractor = WADExtractor(manifest)

    class _Section:
        path_hash = 0x11
        offset = 0
        compressed_size = 3

    class _FakeHeader:
        def __init__(self):
            self.files = [_Section()]

        @staticmethod
        def _get_hash_for_path(path: str) -> int:
            return 0x11 if path == "a/b/c.bin" else 0x22

        @staticmethod
        def extract_by_section(section, file_path, raw=False, data=None):  # pylint: disable=unused-argument
            return data

    monkeypatch.setattr(WADExtractor, "get_wad_header", lambda self, _wad_file: _FakeHeader())
    monkeypatch.setattr(
        WADExtractor,
        "_read_wad_file_range",
        lambda self, wad_file, start, length: b"BIN",  # pylint: disable=unused-argument
    )

    outputs = extractor.extract_files_to_disk({wad_file.name: ["a/b/c.bin"]}, output_dir=str(tmp_path))
    target = outputs[wad_file.name]["a/b/c.bin"]
    assert target is not None
    assert Path(target).is_file()
    assert Path(target).read_bytes() == b"BIN"


def test_load_game_data_for_non_default_region(monkeypatch):
    def _fake_http_get_json(url: str):
        assert "version-sets/KR" in url
        return {
            "releases": [
                _make_game_release("14.2.0+meta", "https://example.invalid/kr-1420.manifest"),
                _make_game_release("14.2.1+meta", "https://example.invalid/kr-1421.manifest"),
            ]
        }

    monkeypatch.setattr("riotmanifest.game.metadata.http_get_json", _fake_http_get_json)

    data = LeagueManifestResolver()
    data.load_game_data(regions=["KR"])
    with pytest.warns(FutureWarning, match="latest_game\\(\\) 已弃用"):
        latest = data.latest_game("KR")
    assert latest is not None
    assert latest["version"] == "14.2.1"
    assert latest["url"] == "https://example.invalid/kr-1421.manifest"


def test_build_game_extractor_requires_live_region(monkeypatch):
    monkeypatch.setattr("riotmanifest.game.metadata.http_get_json", lambda url: {})

    data = LeagueManifestResolver()
    with pytest.raises(LiveConfigNotFoundError, match="EUW"):
        data.build_game_extractor("EUW")


def test_build_game_extractor_uses_resolved_pair(monkeypatch):
    captured = {}

    class _DummyManifest:
        def __init__(self, file, path, **kwargs):  # pylint: disable=unused-argument
            self.file = file
            self.path = path

    class _DummyExtractor:
        def __init__(self, manifest, **kwargs):
            captured["manifest"] = manifest
            captured["kwargs"] = kwargs

    monkeypatch.setattr("riotmanifest.game.factory.PatcherManifest", _DummyManifest)
    monkeypatch.setattr("riotmanifest.game.factory.WADExtractor", _DummyExtractor)
    monkeypatch.setattr(
        LeagueManifestResolver,
        "resolve_live_manifest_pair",
        lambda self, region, match_mode=VersionMatchMode.IGNORE_REVISION: types.SimpleNamespace(
            game=types.SimpleNamespace(url="https://example.invalid/euw-live.manifest")
        ),
    )

    data = LeagueManifestResolver()
    extractor = data.build_game_extractor("EUW", cache_max_entries=64)
    assert isinstance(extractor, _DummyExtractor)
    assert isinstance(captured["manifest"], _DummyManifest)
    assert captured["manifest"].file == "https://example.invalid/euw-live.manifest"
    assert captured["manifest"].path == ""
    assert captured["kwargs"]["cache_max_entries"] == 64


def test_load_lcu_and_build_extractor(monkeypatch):
    captured = {}

    class _DummyManifest:
        def __init__(self, file, path, **kwargs):  # pylint: disable=unused-argument
            self.file = file
            self.path = path

    class _DummyExtractor:
        def __init__(self, manifest, **kwargs):
            captured["manifest"] = manifest
            captured["kwargs"] = kwargs

    def _fake_http_get_json(url: str):
        assert "clientconfig.rpg.riotgames.com" in url
        return {
            "league.live": {
                "platforms": {
                    "win": {
                        "configurations": [
                            {
                                "id": "EUW",
                                "patch_url": "https://example.invalid/lcu-euw.manifest",
                                "metadata": {"theme_manifest": "https://example.invalid/releases/14.4.1/theme/data"},
                            }
                        ]
                    }
                }
            }
        }

    monkeypatch.setattr("riotmanifest.game.metadata.http_get_json", _fake_http_get_json)
    monkeypatch.setattr("riotmanifest.game.factory.PatcherManifest", _DummyManifest)
    monkeypatch.setattr("riotmanifest.game.factory.WADExtractor", _DummyExtractor)

    data = LeagueManifestResolver()
    data.load_lcu_data()
    with pytest.warns(FutureWarning, match="latest_lcu\\(\\) 已弃用"):
        latest = data.latest_lcu("EUW")
    assert latest is not None
    assert latest["version"] == "14.4.1"

    extractor = data.build_lcu_extractor("EUW", cache_max_bytes=1024)
    assert isinstance(extractor, _DummyExtractor)
    assert isinstance(captured["manifest"], _DummyManifest)
    assert captured["manifest"].file == "https://example.invalid/lcu-euw.manifest"
    assert captured["manifest"].path == ""
    assert captured["kwargs"]["cache_max_bytes"] == 1024


def test_resolve_live_manifest_pair_prefers_exact_build(monkeypatch):
    def _fake_http_get_json(url: str):
        if "clientconfig.rpg.riotgames.com" in url:
            return {
                "league.live": {
                    "platforms": {
                        "win": {
                            "configurations": [
                                {
                                    "id": "EUW",
                                    "patch_url": "https://example.invalid/lcu-euw.manifest",
                                    "metadata": {
                                        "theme_manifest": "https://example.invalid/channels/public/rccontent/theme/16.5/EUW/manifest.json"
                                    },
                                    "patch_artifacts": [
                                        {
                                            "id": "game_client",
                                            "type": "patchsieve",
                                            "patchsieve": {
                                                "version_set": "EUW1",
                                                "parameters": {
                                                    "artifact_type_id": "lol-game-client",
                                                    "platform": "windows",
                                                },
                                            },
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                }
            }
        assert "version-sets/EUW1" in url
        return {
            "releases": [
                _make_game_release("16.5.7496037+meta", "https://example.invalid/game-7496037.manifest"),
                _make_game_release("16.5.7511533+meta", "https://example.invalid/game-7511533.manifest"),
            ]
        }

    monkeypatch.setattr("riotmanifest.game.metadata.http_get_json", _fake_http_get_json)

    data = LeagueManifestResolver()
    monkeypatch.setattr(
        data._lcu_version_resolver,
        "resolve",
        lambda manifest_url: VersionInfo(
            display_version="16.5.751.1533",
            normalized_build="16.5.7511533",
            patch_version="16.5",
        ),
    )

    pair = data.resolve_live_manifest_pair("EUW")

    assert pair.lcu.url == "https://example.invalid/lcu-euw.manifest"
    assert pair.game.url == "https://example.invalid/game-7511533.manifest"
    assert str(pair.version) == "16.5"
    assert pair.version.lcu.display_version == "16.5.751.1533"
    assert pair.version.game.display_version == "16.5.7511533"
    assert pair.is_exact_match is True
    assert pair.match_reason == "normalized_build_match"
    assert pair.candidate_count == 2


def test_resolve_live_manifest_pair_ignore_revision_fallback(monkeypatch):
    def _fake_http_get_json(url: str):
        if "clientconfig.rpg.riotgames.com" in url:
            return {
                "league.live": {
                    "platforms": {
                        "win": {
                            "configurations": [
                                {
                                    "id": "EUW",
                                    "patch_url": "https://example.invalid/lcu-euw.manifest",
                                    "metadata": {
                                        "theme_manifest": "https://example.invalid/channels/public/rccontent/theme/16.5/EUW/manifest.json"
                                    },
                                    "patch_artifacts": [
                                        {
                                            "id": "game_client",
                                            "type": "patchsieve",
                                            "patchsieve": {
                                                "version_set": "EUW1",
                                                "parameters": {
                                                    "artifact_type_id": "lol-game-client",
                                                    "platform": "windows",
                                                },
                                            },
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                }
            }
        assert "version-sets/EUW1" in url
        return {
            "releases": [
                _make_game_release("16.5.7496037+meta", "https://example.invalid/game-7496037.manifest"),
                _make_game_release("16.5.7511533+meta", "https://example.invalid/game-7511533.manifest"),
                _make_game_release("16.5.7519084+meta", "https://example.invalid/game-7519084.manifest"),
            ]
        }

    monkeypatch.setattr("riotmanifest.game.metadata.http_get_json", _fake_http_get_json)

    data = LeagueManifestResolver()
    monkeypatch.setattr(
        data._lcu_version_resolver,
        "resolve",
        lambda manifest_url: VersionInfo(
            display_version="16.5.751.8496",
            normalized_build="16.5.7518496",
            patch_version="16.5",
        ),
    )

    pair = data.resolve_live_manifest_pair(
        "EUW",
        match_mode=VersionMatchMode.IGNORE_REVISION,
    )

    assert pair.game.url == "https://example.invalid/game-7511533.manifest"
    assert str(pair.version) == "16.5"
    assert pair.is_exact_match is False
    assert pair.match_reason == "ignore_revision_fallback"


def test_resolve_live_manifest_pair_defaults_to_ignore_revision(monkeypatch):
    def _fake_http_get_json(url: str):
        if "clientconfig.rpg.riotgames.com" in url:
            return {
                "league.live": {
                    "platforms": {
                        "win": {
                            "configurations": [
                                {
                                    "id": "EUW",
                                    "patch_url": "https://example.invalid/lcu-euw.manifest",
                                    "metadata": {
                                        "theme_manifest": "https://example.invalid/channels/public/rccontent/theme/16.5/EUW/manifest.json"
                                    },
                                    "patch_artifacts": [
                                        {
                                            "id": "game_client",
                                            "type": "patchsieve",
                                            "patchsieve": {
                                                "version_set": "EUW1",
                                                "parameters": {
                                                    "artifact_type_id": "lol-game-client",
                                                    "platform": "windows",
                                                },
                                            },
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                }
            }
        return {
            "releases": [
                _make_game_release("16.5.7496037+meta", "https://example.invalid/game-7496037.manifest"),
                _make_game_release("16.5.7511533+meta", "https://example.invalid/game-7511533.manifest"),
                _make_game_release("16.5.7519084+meta", "https://example.invalid/game-7519084.manifest"),
            ]
        }

    monkeypatch.setattr("riotmanifest.game.metadata.http_get_json", _fake_http_get_json)

    data = LeagueManifestResolver()
    monkeypatch.setattr(
        data._lcu_version_resolver,
        "resolve",
        lambda manifest_url: VersionInfo(
            display_version="16.5.751.8496",
            normalized_build="16.5.7518496",
            patch_version="16.5",
        ),
    )

    pair = data.resolve_live_manifest_pair("EUW")

    assert pair.game.url == "https://example.invalid/game-7511533.manifest"
    assert pair.match_mode is VersionMatchMode.IGNORE_REVISION
    assert pair.match_reason == "ignore_revision_fallback"


def test_resolve_live_manifest_pair_ignore_revision_raises_when_all_patch_candidates_newer(monkeypatch):
    def _fake_http_get_json(url: str):
        if "clientconfig.rpg.riotgames.com" in url:
            return {
                "league.live": {
                    "platforms": {
                        "win": {
                            "configurations": [
                                {
                                    "id": "EUW",
                                    "patch_url": "https://example.invalid/lcu-euw.manifest",
                                    "metadata": {
                                        "theme_manifest": "https://example.invalid/channels/public/rccontent/theme/16.5/EUW/manifest.json"
                                    },
                                    "patch_artifacts": [
                                        {
                                            "id": "game_client",
                                            "type": "patchsieve",
                                            "patchsieve": {
                                                "version_set": "EUW1",
                                                "parameters": {
                                                    "artifact_type_id": "lol-game-client",
                                                    "platform": "windows",
                                                },
                                            },
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                }
            }
        return {
            "releases": [
                _make_game_release("16.5.7511533+meta", "https://example.invalid/game-7511533.manifest"),
                _make_game_release("16.5.7519084+meta", "https://example.invalid/game-7519084.manifest"),
            ]
        }

    monkeypatch.setattr("riotmanifest.game.metadata.http_get_json", _fake_http_get_json)

    data = LeagueManifestResolver()
    monkeypatch.setattr(
        data._lcu_version_resolver,
        "resolve",
        lambda manifest_url: VersionInfo(
            display_version="16.5.750.9999",
            normalized_build="16.5.7509999",
            patch_version="16.5",
        ),
    )

    with pytest.raises(
        ConsistentGameManifestNotFoundError,
        match="没有不高于 LCU build 16.5.7509999 的 GAME manifest",
    ):
        data.resolve_live_manifest_pair(
            "EUW",
            match_mode=VersionMatchMode.IGNORE_REVISION,
        )


def test_resolve_live_manifest_pair_patch_latest_picks_newest_same_patch(monkeypatch):
    def _fake_http_get_json(url: str):
        if "clientconfig.rpg.riotgames.com" in url:
            return {
                "league.live": {
                    "platforms": {
                        "win": {
                            "configurations": [
                                {
                                    "id": "EUW",
                                    "patch_url": "https://example.invalid/lcu-euw.manifest",
                                    "metadata": {
                                        "theme_manifest": "https://example.invalid/channels/public/rccontent/theme/16.5/EUW/manifest.json"
                                    },
                                    "patch_artifacts": [
                                        {
                                            "id": "game_client",
                                            "type": "patchsieve",
                                            "patchsieve": {
                                                "version_set": "EUW1",
                                                "parameters": {
                                                    "artifact_type_id": "lol-game-client",
                                                    "platform": "windows",
                                                },
                                            },
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                }
            }
        return {
            "releases": [
                _make_game_release("16.5.7496037+meta", "https://example.invalid/game-7496037.manifest"),
                _make_game_release("16.5.7511533+meta", "https://example.invalid/game-7511533.manifest"),
                _make_game_release("16.5.7519084+meta", "https://example.invalid/game-7519084.manifest"),
            ]
        }

    monkeypatch.setattr("riotmanifest.game.metadata.http_get_json", _fake_http_get_json)

    data = LeagueManifestResolver()
    monkeypatch.setattr(
        data._lcu_version_resolver,
        "resolve",
        lambda manifest_url: VersionInfo(
            display_version="16.5.751.8496",
            normalized_build="16.5.7518496",
            patch_version="16.5",
        ),
    )

    pair = data.resolve_live_manifest_pair(
        "EUW",
        match_mode=VersionMatchMode.PATCH_LATEST,
    )

    assert pair.game.url == "https://example.invalid/game-7519084.manifest"
    assert pair.is_exact_match is False
    assert pair.match_reason == "patch_latest_fallback"


def test_resolve_live_manifest_pair_strict_raises_without_exact_match(monkeypatch):
    def _fake_http_get_json(url: str):
        if "clientconfig.rpg.riotgames.com" in url:
            return {
                "league.live": {
                    "platforms": {
                        "win": {
                            "configurations": [
                                {
                                    "id": "EUW",
                                    "patch_url": "https://example.invalid/lcu-euw.manifest",
                                    "metadata": {
                                        "theme_manifest": "https://example.invalid/channels/public/rccontent/theme/16.5/EUW/manifest.json"
                                    },
                                    "patch_artifacts": [
                                        {
                                            "id": "game_client",
                                            "type": "patchsieve",
                                            "patchsieve": {
                                                "version_set": "EUW1",
                                                "parameters": {
                                                    "artifact_type_id": "lol-game-client",
                                                    "platform": "windows",
                                                },
                                            },
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                }
            }
        return {
            "releases": [
                _make_game_release("16.5.7496037+meta", "https://example.invalid/game-7496037.manifest"),
            ]
        }

    monkeypatch.setattr("riotmanifest.game.metadata.http_get_json", _fake_http_get_json)

    data = LeagueManifestResolver()
    monkeypatch.setattr(
        data._lcu_version_resolver,
        "resolve",
        lambda manifest_url: VersionInfo(
            display_version="16.5.751.1533",
            normalized_build="16.5.7511533",
            patch_version="16.5",
        ),
    )

    with pytest.raises(ConsistentGameManifestNotFoundError, match="16.5.7511533"):
        data.resolve_live_manifest_pair(
            "EUW",
            match_mode=VersionMatchMode.STRICT,
        )


def test_extract_windows_version_from_utf16_payload():
    payload = b"prefix" + "FileVersion".encode("utf-16le") + b"\x00\x00" + "16.5.751.1533".encode("utf-16le") + b"suffix"

    assert LeagueManifestResolver()._lcu_version_resolver._extract_windows_version(payload) == "16.5.751.1533"


def test_resolved_version_supports_multiple_display_modes():
    data = VersionInfo(
        display_version="16.5.751.1533",
        normalized_build="16.5.7511533",
        patch_version="16.5",
    )
    game = VersionInfo(
        display_version="16.5.7511533",
        normalized_build="16.5.7511533",
        patch_version="16.5",
    )

    from riotmanifest.game import ResolvedVersion

    resolved = ResolvedVersion(lcu=data, game=game)

    assert str(resolved) == "16.5"
    assert str(resolved.with_display_mode(VersionDisplayMode.LCU)) == "16.5.751.1533"
    assert str(resolved.with_display_mode(VersionDisplayMode.GAME)) == "16.5.7511533"
