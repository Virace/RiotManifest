"""类名兼容层测试."""

import pytest

from riotmanifest.game import LeagueManifestResolver, RiotGameData


def test_riot_game_data_alias_warns() -> None:
    """旧类名实例化时应发出弃用提示."""
    with pytest.warns(FutureWarning, match="RiotGameData 已弃用"):
        resolver = RiotGameData()

    assert isinstance(resolver, LeagueManifestResolver)
