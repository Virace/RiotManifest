"""真实网络的下载 + 修复 + 增量更新端到端冒烟脚本（固定清单版本）.

不进入 pytest 收集范围，需单独执行：

    uv run python scripts/e2e_update_real.py

清单链接固定为两个相邻正式版本（索引来源 Morilli/riot-manifests 仓库，
本仓库只保存链接不引入其内容）。16.3→16.4 恰好跨越 Riot 的
HKDF→BLAKE3 哈希迁移边界，是最有代表性的更新场景之一。

流程：全量下载少量小文件 → 人为损坏后 REPAIR 修复 → 跨版本增量更新，
每一步都以 chunk 级哈希验证收尾；断言失败即非零退出。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from riotmanifest import ManifestArchive, ManifestUpdater, PatcherManifest, SyncMode  # noqa: E402
from riotmanifest.update import FileAction, verify_file_chunks  # noqa: E402

# 16.3.7457600 lol-game-client
OLD_MANIFEST_URL = "https://lol.secure.dyn.riotcdn.net/channels/public/releases/65F094ADF9A65AD2.manifest"
# 16.4.7480682 lol-game-client
NEW_MANIFEST_URL = "https://lol.secure.dyn.riotcdn.net/channels/public/releases/BA80B75282F55531.manifest"

TARGET_FILE_COUNT = 3
MIN_FILE_BYTES = 512 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024


def _pick_targets(old_manifest: PatcherManifest, new_manifest: PatcherManifest) -> list[str]:
    """挑选两个版本都存在的小体积 zh_CN wad 文件（按旧版本大小升序）."""
    candidates = [
        file
        for file in old_manifest.filter_files(flag="zh_CN", pattern="wad.client")
        if not file.link
        and MIN_FILE_BYTES <= file.size <= MAX_FILE_BYTES
        and len(file.chunks) >= 3
        and file.name in new_manifest.files
    ]
    candidates.sort(key=lambda file: file.size)
    return [file.name for file in candidates[:TARGET_FILE_COUNT]]


def _mb(size: int) -> str:
    return f"{size / 1024 / 1024:.2f}MB"


def main() -> None:
    """执行三段式冒烟：全量下载 → REPAIR 修复 → 跨版本增量更新."""
    with tempfile.TemporaryDirectory(prefix="riotmanifest_update_e2e_") as out_dir:
        old_manifest = PatcherManifest(file=OLD_MANIFEST_URL, path=out_dir)
        new_manifest = PatcherManifest(file=NEW_MANIFEST_URL, path=out_dir)

        target_names = _pick_targets(old_manifest, new_manifest)
        assert len(target_names) == TARGET_FILE_COUNT, f"候选目标文件不足: {target_names}"
        print(f"[E2E] 目标文件: {target_names}")

        old_files = [old_manifest.files[name] for name in target_names]
        new_files = [new_manifest.files[name] for name in target_names]
        archive = ManifestArchive(out_dir)

        # 1. 全量下载（无本地状态）。
        result_initial = asyncio.run(ManifestUpdater(old_manifest).sync(old_files))
        assert result_initial.failed == [], f"全量下载失败: {result_initial.failed}"
        assert result_initial.downloaded_bytes > 0
        for file in old_files:
            check = verify_file_chunks(file, os.path.join(out_dir, file.name))
            assert check.complete, f"全量下载后校验失败: {file.name}"
        state = archive.load_installed()
        assert state is not None and state.manifest_id == f"{old_manifest.manifest_id:016X}"
        print(f"[E2E] 全量: downloaded={_mb(result_initial.downloaded_bytes)} -> installed={state.manifest_id}")

        # 2. 人为损坏一个文件中部 → REPAIR 应只补坏块。
        corrupt_file = old_files[0]
        corrupt_path = Path(out_dir) / corrupt_file.name
        with open(corrupt_path, "r+b") as f:
            f.seek(corrupt_file.size // 2)
            f.write(b"X" * 16)

        result_repair = asyncio.run(ManifestUpdater(old_manifest).sync(old_files, mode=SyncMode.REPAIR))
        assert result_repair.failed == []
        assert 0 < result_repair.downloaded_bytes < corrupt_file.size, (
            f"修复应只补坏块: downloaded={result_repair.downloaded_bytes}, file_size={corrupt_file.size}"
        )
        check = verify_file_chunks(corrupt_file, corrupt_path)
        assert check.complete, "修复后校验失败"
        print(f"[E2E] 修复: downloaded={result_repair.downloaded_bytes}B（文件 {corrupt_file.size}B）")

        # 3. 跨版本增量更新（旧清单自动从 installed.json 存档解析）。
        result_update = asyncio.run(ManifestUpdater(new_manifest).sync(new_files))
        assert result_update.failed == [], f"更新失败: {result_update.failed}"
        for file in new_files:
            check = verify_file_chunks(file, os.path.join(out_dir, file.name))
            assert check.complete, f"更新后校验失败: {file.name}"
        state = archive.load_installed()
        assert state is not None and state.manifest_id == f"{new_manifest.manifest_id:016X}"

        # 未变化文件应被文件级跳过（哈希迁移边界上未变化文件可能极少，容忍为空）。
        unchanged = [
            name
            for name in target_names
            if old_manifest.files[name].hexdigest() == new_manifest.files[name].hexdigest()
        ]
        for name in unchanged:
            assert result_update.actions[name] == FileAction.SKIP
        total_new_bytes = sum(file.size for file in new_files)
        print(
            f"[E2E] 更新: downloaded={_mb(result_update.downloaded_bytes)} "
            f"reused={_mb(result_update.reused_bytes)} total={_mb(total_new_bytes)} "
            f"unchanged={len(unchanged)}/{len(target_names)} -> installed={state.manifest_id}"
        )

        print("[E2E] 全部通过")


if __name__ == "__main__":
    main()
