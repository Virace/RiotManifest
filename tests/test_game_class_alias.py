"""类名兼容层测试."""

import pytest

from riotmanifest.game import (
    LeagueManifestError,
    LeagueManifestResolver,
    LiveConfigNotFoundError,
    LiveManifestPair,
    PatchlineConfigNotFoundError,
    RegionConfigNotFoundError,
    ResolvedManifestPair,
    RiotGameData,
    RiotGameDataError,
)


def test_riot_game_data_alias_warns() -> None:
    """旧类名实例化时应发出弃用提示."""
    with pytest.warns(FutureWarning, match="RiotGameData 已弃用"):
        resolver = RiotGameData()

    assert isinstance(resolver, LeagueManifestResolver)


def test_error_and_pair_aliases_remain_available() -> None:
    """新旧命名应保持兼容别名关系."""
    assert RiotGameDataError is LeagueManifestError
    assert PatchlineConfigNotFoundError is RegionConfigNotFoundError
    assert LiveConfigNotFoundError is RegionConfigNotFoundError
    assert LiveManifestPair is ResolvedManifestPair
