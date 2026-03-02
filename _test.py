"""手动验证脚本（非 pytest）.

用途：
1. 按需提取 WAD 内多个小文件，验证“部分缺失可接受”场景。
2. 快速查看 GAME/LCU 区域元数据加载是否正常。
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from riotmanifest import PatcherManifest, RiotGameData, WADExtractor, diff_manifests, diff_wad_headers

MANIFEST_URL = "https://lol.secure.dyn.riotcdn.net/channels/public/releases/BA80B75282F55531.manifest"
DEFAULT_WAD = "DATA/FINAL/Champions/Annie.wad.client"
# 16.3.7457600
OLD_MANIFEST_URL = "https://lol.secure.dyn.riotcdn.net/channels/public/releases/65F094ADF9A65AD2.manifest"

# 16.4.7480682
NEW_MANIFEST_URL = "https://lol.secure.dyn.riotcdn.net/channels/public/releases/BA80B75282F55531.manifest"
FOCUS_TARGET_FILES = [
    "DATA/FINAL/Champions/Renata.zh_CN.wad.client",
]
DIFF_OUTPUT_JSON = Path("out") / "manifest_diff_renata_16_3_to_16_4.json"
DIFF_COLLAPSE_EQUAL_PAIRS = True

MANIFEST_16_3_URL = "https://lol.secure.dyn.riotcdn.net/channels/public/releases/9FE07DA11C89FD5E.manifest"
MANIFEST_16_4_URL = "https://lol.secure.dyn.riotcdn.net/channels/public/releases/BA80B75282F55531.manifest"
AKSHAN_WAD_PATH = "DATA/FINAL/Champions/Akshan.zh_CN.wad.client"
AKSHAN_WAD_DIFF_OUTPUT_JSON = Path("out") / "wad_header_diff_akshan_16_3_to_16_4.json"


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


def test_diff(output_json: Path = DIFF_OUTPUT_JSON) -> None:
    """手动验证 Manifest 差异并导出美化 JSON。."""
    rep = diff_manifests(
        OLD_MANIFEST_URL,
        NEW_MANIFEST_URL,
        flags="zh_CN",
        include_unchanged=False,
        hash_type_mismatch_mode="loose",
    )
    saved_path = rep.dump_pretty_json(
        output_json,
        collapse_equal_pairs=DIFF_COLLAPSE_EQUAL_PAIRS,
    )
    print("diff summary:", rep.summary)
    print("changed files:", [entry.path for entry in rep.changed])
    print("unchanged files:", [entry.path for entry in rep.unchanged])
    print("json saved:", saved_path)


def test_akshan_wad_header_diff(output_json: Path = AKSHAN_WAD_DIFF_OUTPUT_JSON) -> None:
    """手动验证 Akshan WAD 头差异并导出美化 JSON。."""
    manifest_report = diff_manifests(
        MANIFEST_16_3_URL,
        MANIFEST_16_4_URL,
        flags="zh_CN",
        target_files=[AKSHAN_WAD_PATH],
        include_unchanged=True,
        detect_moves=False,
        hash_type_mismatch_mode="loose",
    )
    report = diff_wad_headers(
        manifest_report=manifest_report,
        target_wad_files=[AKSHAN_WAD_PATH],
        hash_type_mismatch_mode="loose",
        include_unchanged=False,
    )
    saved_path = report.dump_pretty_json(
        output_json,
        collapse_manifest_equal_pairs=DIFF_COLLAPSE_EQUAL_PAIRS,
    )

    print("wad header diff summary:", report.summary)
    if report.files:
        entry = report.files[0]
        changed_count = sum(1 for diff in entry.section_diffs if diff.status == "changed")
        added_count = sum(1 for diff in entry.section_diffs if diff.status == "added")
        removed_count = sum(1 for diff in entry.section_diffs if diff.status == "removed")
        unchanged_count = sum(1 for diff in entry.section_diffs if diff.status == "unchanged")
        print("target wad:", entry.wad_path)
        print("wad status:", entry.status)
        print(
            "section stats:",
            {
                "changed": changed_count,
                "added": added_count,
                "removed": removed_count,
                "unchanged": unchanged_count,
            },
        )
    print("json saved:", saved_path)


def main() -> None:
    """脚本主入口。."""
    # extra_test()
    # test_game_data()
    # test_diff()
    test_akshan_wad_header_diff()


if __name__ == "__main__":
    main()
