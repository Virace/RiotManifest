import re
import types
from pathlib import Path

import pytest

from riotmanifest.extractor import WADExtractor
from riotmanifest.game import (
    ConsistentGameManifestNotFoundError,
    LcuVersionUnavailableError,
    LeagueManifestResolver,
    RegionConfigNotFoundError,
    VersionDisplayMode,
    VersionInfo,
    VersionMatchMode,
)
from riotmanifest.game.factory import _build_game_version_info, _build_lcu_version_info
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
                _make_game_release("14.2.1234500+meta", "https://example.invalid/kr-1420.manifest"),
                _make_game_release("14.2.1234501+meta", "https://example.invalid/kr-1421.manifest"),
            ]
        }

    monkeypatch.setattr("riotmanifest.game.metadata.http_get_json", _fake_http_get_json)

    data = LeagueManifestResolver()
    data.load_game_data(regions=["KR"])
    with pytest.warns(FutureWarning, match="latest_game\\(\\) 已弃用"):
        latest = data.latest_game("KR")
    assert latest is not None
    assert latest["version"] == "14.2.1234501"
    assert latest["url"] == "https://example.invalid/kr-1421.manifest"


def test_build_game_extractor_requires_live_region(monkeypatch):
    monkeypatch.setattr("riotmanifest.game.metadata.http_get_json", lambda url: {})

    data = LeagueManifestResolver()
    with pytest.raises(RegionConfigNotFoundError, match="EUW"):
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
        "resolve_manifest_pair",
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


def test_resolve_manifest_pair_prefers_exact_build(monkeypatch):
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
            normalized_build="16.5.7511533",
            patch_version="16.5",
            exe_version="16.5.751.1533",
        ),
    )

    pair = data.resolve_manifest_pair("EUW")

    assert pair.lcu.url == "https://example.invalid/lcu-euw.manifest"
    assert pair.game.url == "https://example.invalid/game-7511533.manifest"
    assert str(pair.version) == "16.5"
    assert pair.version.lcu.display_version == "16.5.751.1533"
    assert pair.version.game.display_version == "16.5.7511533"
    assert pair.is_exact_match is True
    assert pair.match_reason == "normalized_build_match"
    assert pair.candidate_count == 2


def test_resolve_manifest_pair_ignore_revision_fallback(monkeypatch):
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
            normalized_build="16.5.7518496",
            patch_version="16.5",
            exe_version="16.5.751.8496",
        ),
    )

    pair = data.resolve_manifest_pair(
        "EUW",
        match_mode=VersionMatchMode.IGNORE_REVISION,
    )

    assert pair.game.url == "https://example.invalid/game-7511533.manifest"
    assert str(pair.version) == "16.5"
    assert pair.is_exact_match is False
    assert pair.match_reason == "ignore_revision_fallback"
    assert pair.region == "EUW"


def test_resolve_manifest_pair_supports_pbe_region_alias(monkeypatch):
    captured = {}

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
                },
                "league.pbe": {
                    "platforms": {
                        "win": {
                            "configurations": [
                                {
                                    "id": "PBE",
                                    "patch_url": "https://example.invalid/lcu-pbe.manifest",
                                    "metadata": {
                                        "theme_manifest": "https://example.invalid/channels/public/rccontent/theme/16.6/PBE/manifest.json"
                                    },
                                    "patch_artifacts": [
                                        {
                                            "id": "game_client",
                                            "type": "patchsieve",
                                            "patchsieve": {
                                                "version_set": "PBE1",
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
                },
            }
        captured["url"] = url
        assert "version-sets/PBE1" in url
        return {
            "releases": [
                _make_game_release("16.6.7517822+meta", "https://example.invalid/game-pbe.manifest"),
            ]
        }

    monkeypatch.setattr("riotmanifest.game.metadata.http_get_json", _fake_http_get_json)

    data = LeagueManifestResolver()
    monkeypatch.setattr(
        data._lcu_version_resolver,
        "resolve",
        lambda manifest_url: VersionInfo(
            normalized_build="16.6.7517822",
            patch_version="16.6",
            exe_version="16.6.751.7822",
        ),
    )

    pair = data.resolve_manifest_pair("PBE")

    assert pair.region == "PBE"
    assert pair.lcu.url == "https://example.invalid/lcu-pbe.manifest"
    assert pair.game.url == "https://example.invalid/game-pbe.manifest"
    assert "q%5Bartifact_type_id%5D=lol-game-client" in captured["url"]
    assert "q%5Bplatform%5D=windows" in captured["url"]


def test_resolve_manifest_pair_defaults_to_ignore_revision(monkeypatch):
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
            normalized_build="16.5.7518496",
            patch_version="16.5",
            exe_version="16.5.751.8496",
        ),
    )

    pair = data.resolve_manifest_pair("EUW")

    assert pair.game.url == "https://example.invalid/game-7511533.manifest"
    assert pair.match_mode is VersionMatchMode.IGNORE_REVISION
    assert pair.match_reason == "ignore_revision_fallback"


def test_resolve_manifest_pair_ignore_revision_raises_when_all_patch_candidates_newer(monkeypatch):
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
            normalized_build="16.5.7509999",
            patch_version="16.5",
            exe_version="16.5.750.9999",
        ),
    )

    with pytest.raises(
        ConsistentGameManifestNotFoundError,
        match="没有不高于 LCU build 16.5.7509999 的 GAME manifest",
    ):
        data.resolve_manifest_pair(
            "EUW",
            match_mode=VersionMatchMode.IGNORE_REVISION,
        )


def test_resolve_manifest_pair_patch_latest_picks_newest_same_patch(monkeypatch):
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
            normalized_build="16.5.7518496",
            patch_version="16.5",
            exe_version="16.5.751.8496",
        ),
    )

    pair = data.resolve_manifest_pair(
        "EUW",
        match_mode=VersionMatchMode.PATCH_LATEST,
    )

    assert pair.game.url == "https://example.invalid/game-7519084.manifest"
    assert pair.is_exact_match is False
    assert pair.match_reason == "patch_latest_fallback"


def test_resolve_manifest_pair_strict_raises_without_exact_match(monkeypatch):
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
            normalized_build="16.5.7511533",
            patch_version="16.5",
            exe_version="16.5.751.1533",
        ),
    )

    with pytest.raises(ConsistentGameManifestNotFoundError, match="16.5.7511533"):
        data.resolve_manifest_pair(
            "EUW",
            match_mode=VersionMatchMode.STRICT,
        )


def test_extract_windows_version_from_utf16_payload():
    payload = b"prefix" + "FileVersion".encode("utf-16le") + b"\x00\x00" + "16.5.751.1533".encode("utf-16le") + b"suffix"

    assert LeagueManifestResolver()._lcu_version_resolver._extract_windows_version(payload) == "16.5.751.1533"


def test_build_game_version_info_normalizes_metadata_version():
    version = _build_game_version_info("16.5.7511533+branch.releases-16-5.content.release")

    assert version.metadata_version == "16.5.7511533"
    assert version.exe_version is None
    assert version.compact_version == "16.5.7511533"
    assert version.dotted_version == "16.5.751.1533"
    assert version.patch_version == "16.5"


def test_build_lcu_version_info_requires_four_segment_exe_version():
    with pytest.raises(LcuVersionUnavailableError, match="第四段不满足 4 位约束"):
        _build_lcu_version_info("16.5.751.33")


def test_resolved_version_supports_multiple_display_modes():
    data = VersionInfo(
        normalized_build="16.5.7511533",
        patch_version="16.5",
        exe_version="16.5.751.1533",
    )
    game = VersionInfo(
        normalized_build="16.5.7511533",
        patch_version="16.5",
        metadata_version="16.5.7511533",
    )

    from riotmanifest.game import ResolvedVersion

    resolved = ResolvedVersion(lcu=data, game=game)

    assert str(resolved) == "16.5"
    assert str(resolved.with_display_mode(VersionDisplayMode.LCU)) == "16.5.751.1533"
    assert str(resolved.with_display_mode(VersionDisplayMode.GAME)) == "16.5.7511533"


def test_lcu_version_resolver_caches_by_manifest_url(monkeypatch: pytest.MonkeyPatch) -> None:
    import riotmanifest.game.factory as game_factory

    calls = {"count": 0}

    class _DummyManifest:
        def __init__(self, file: str, path: str) -> None:
            self.file = file
            self.path = path
            self.files = {}

    def _fake_resolve_from_manifest(self, manifest, temp_dir: Path):
        calls["count"] += 1
        assert manifest.file == "https://example.invalid/lcu.manifest"
        assert temp_dir.exists()
        return VersionInfo(
            normalized_build="16.5.7518496",
            patch_version="16.5",
            exe_version="16.5.751.8496",
        )

    monkeypatch.setattr(game_factory, "PatcherManifest", _DummyManifest)
    monkeypatch.setattr(
        game_factory._LcuVersionResolver,
        "_resolve_from_manifest",
        _fake_resolve_from_manifest,
    )

    resolver = game_factory._LcuVersionResolver()
    first = resolver.resolve("https://example.invalid/lcu.manifest")
    second = resolver.resolve("https://example.invalid/lcu.manifest")

    assert first is second
    assert calls["count"] == 1


def test_lcu_version_resolver_supports_macos_plist(monkeypatch: pytest.MonkeyPatch) -> None:
    import riotmanifest.game.factory as game_factory

    resolver = game_factory._LcuVersionResolver()
    manifest = object.__new__(PatcherManifest)
    manifest.file = "https://example.invalid/mac.manifest"
    manifest.files = {
        "Contents/LoL/LeagueClient.app/Contents/Info.plist": object(),
    }

    monkeypatch.setattr(
        game_factory._LcuVersionResolver,
        "_download_manifest_file",
        lambda self, manifest, target_file, temp_dir: b"plist_payload",
    )
    monkeypatch.setattr(
        game_factory._LcuVersionResolver,
        "_extract_macos_version",
        lambda self, payload: "16.5.751.8496",
    )

    version = resolver._resolve_from_manifest(manifest=manifest, temp_dir=Path("/tmp"))

    assert version.normalized_build == "16.5.7518496"
    assert version.exe_version == "16.5.751.8496"


def test_lcu_version_resolver_requires_precise_version_carrier(monkeypatch: pytest.MonkeyPatch) -> None:
    import riotmanifest.game.factory as game_factory

    resolver = game_factory._LcuVersionResolver()
    manifest = object.__new__(PatcherManifest)
    manifest.file = "https://example.invalid/lcu.manifest"
    manifest.files = {}

    monkeypatch.setattr(
        game_factory._LcuVersionResolver,
        "_extract_patch_version_hint",
        lambda self, manifest, temp_dir: "16.5",
    )
    with pytest.raises(LcuVersionUnavailableError, match="只能解析到补丁版本 16.5"):
        resolver._resolve_from_manifest(manifest=manifest, temp_dir=Path("/tmp"))

    monkeypatch.setattr(
        game_factory._LcuVersionResolver,
        "_extract_patch_version_hint",
        lambda self, manifest, temp_dir: None,
    )
    with pytest.raises(LcuVersionUnavailableError, match="不存在可用的 LCU 版本载体"):
        resolver._resolve_from_manifest(manifest=manifest, temp_dir=Path("/tmp"))


def test_list_game_candidates_requires_game_version_set() -> None:
    import riotmanifest.game.factory as game_factory

    resolver = LeagueManifestResolver()
    resolver._lcu_data["EUW"] = game_factory._RegionConfigRecord(
        canonical_region="EUW",
        patchline="live",
        lcu_config_id="EUW",
        launcher_region="EUW",
        manifest_url="https://example.invalid/lcu-euw.manifest",
        manifest_id="euw",
        version_hint="16.5",
        game_version_set="",
        game_artifact_type="lol-game-client",
        game_platform="windows",
        aliases=("EUW",),
    )
    resolver._region_aliases["EUW"] = "EUW"

    with pytest.raises(RegionConfigNotFoundError, match="GAME version-set"):
        resolver.list_game_candidates("EUW")


def test_latest_game_returns_none_for_versionless_manifest() -> None:
    import riotmanifest.game.factory as game_factory

    resolver = LeagueManifestResolver()
    resolver._game_data["KR"] = [
        game_factory.ManifestRef(
            artifact_group="game",
            region="KR",
            source="sieve",
            url="https://example.invalid/kr.manifest",
            manifest_id="kr",
            version=None,
        )
    ]

    with pytest.warns(FutureWarning, match=r"latest_game\(\) 已弃用"):
        assert resolver.latest_game("KR") is None
