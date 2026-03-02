"""手动验证脚本（非 pytest）.

用途：
1. 按需提取 WAD 内多个小文件，验证“部分缺失可接受”场景。
2. 快速查看 GAME/LCU 区域元数据加载是否正常。
"""

from __future__ import annotations

import sys

from loguru import logger

from riotmanifest import PatcherManifest, RiotGameData, WADExtractor

MANIFEST_URL = "https://lol.secure.dyn.riotcdn.net/channels/public/releases/BA80B75282F55531.manifest"
DEFAULT_WAD = "DATA/FINAL/Champions/Annie.wad.client"


logger.configure(handlers=[{"sink": sys.stdout, "level": "DEBUG"}])
logger.enable("riotmanifest")


def _build_skin_bin_paths(max_skin_id: int = 100) -> list[str]:
    """构造 `skin0.bin ~ skinN.bin` 的测试路径列表."""
    return [f"data/characters/Annie/skins/skin{index}.bin" for index in range(max_skin_id + 1)]


def extra_test(min_success_count: int = 3) -> None:
    """执行 WAD 按需提取验证（非 pytest）.

    判定规则：
    - 允许部分目标文件不存在（返回 `None`）。
    - 只要成功解包数量达到 `min_success_count`，即视为通过。

    Args:
        min_success_count: 最小成功提取文件数量阈值。

    Raises:
        RuntimeError: 当成功提取数量小于阈值时抛出。
    """
    manifest = PatcherManifest(file=MANIFEST_URL, path="")
    extractor = WADExtractor(
        manifest=manifest,
        prefetch_chunk_concurrency=6,
        recommended_max_targets_per_wad=120,
    )

    target_paths = _build_skin_bin_paths(max_skin_id=100)
    outputs = extractor.extract_files({DEFAULT_WAD: target_paths})
    resolved = outputs.get(DEFAULT_WAD, {})

    success_count = sum(1 for data in resolved.values() if data)
    missing_count = len(target_paths) - success_count

    logger.info(
        "WAD提取完成: wad={}, total_targets={}, success={}, missing={}",
        DEFAULT_WAD,
        len(target_paths),
        success_count,
        missing_count,
    )

    if success_count < min_success_count:
        raise RuntimeError(
            f"提取结果未达到阈值: success={success_count}, required={min_success_count}, "
            f"wad={DEFAULT_WAD}, manifest={MANIFEST_URL}"
        )


def test_game_data() -> None:
    """手动验证 GAME/LCU 元数据加载。."""
    rgd = RiotGameData()
    rgd.load_lcu_data()
    rgd.load_game_data()
    print(rgd.latest_lcu())
    print(rgd.latest_game())
    print(rgd.available_lcu_regions())
    print(rgd.available_game_regions())


def main() -> None:
    """脚本主入口。."""
    extra_test()
    test_game_data()


if __name__ == "__main__":
    main()
