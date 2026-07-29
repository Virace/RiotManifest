"""diff 报告驱动的更新计划器.

把 `diff_manifests` 的文件级差异报告映射为每文件动作：

- unchanged → SKIP（整个跳过，不验证）
- changed → PATCH（本地验证 + 补洞）
- added → NEW（全新下载）
- moved → MOVE（本地复制，不下载）
- removed → REMOVE（删除旧清单声明的文件）

SKIP / MOVE / REMOVE 是受管理安装动作，只有 schema 2 状态记录的路径
才能授权；SKIP 与 MOVE 还需通过本地普通文件 + 大小门禁。无旧清单
（report=None）时全部按 PATCH 兜底，由本地验证决定实际下载量。
"""

from __future__ import annotations

import stat
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from riotmanifest.diff.manifest_diff import ManifestDiffReport
    from riotmanifest.manifest import PatcherFile


class FileAction(Enum):
    """更新计划中的单文件动作."""

    SKIP = "skip"
    PATCH = "patch"
    NEW = "new"
    MOVE = "move"
    REMOVE = "remove"


@dataclass(slots=True)
class PlanEntry:
    """单文件计划条目；REMOVE 条目没有对应的新清单文件对象."""

    action: FileAction
    path: str
    file: PatcherFile | None = None
    move_from: str | None = None


@dataclass(slots=True)
class UpdatePlan:
    """按文件组织的更新计划."""

    entries: list[PlanEntry] = field(default_factory=list)

    def by_action(self, action: FileAction) -> list[PlanEntry]:
        """返回指定动作的全部条目."""
        return [entry for entry in self.entries if entry.action == action]


def build_update_plan(
    report: ManifestDiffReport | None,
    files: list[PatcherFile],
    *,
    managed_files: set[str] | frozenset[str] = frozenset(),
    output_dir: str | Path | None = None,
    allow_reuse: bool = True,
) -> UpdatePlan:
    """把 diff 报告映射为更新计划.

    Args:
        report: `diff_manifests(old, new, include_unchanged=True, detect_moves=True)`
            的报告；None 表示无旧清单。
        files: 新清单侧的目标文件列表（调用方已 filter）。
        managed_files: schema 2 状态确认的旧清单文件集合。
        output_dir: 安装输出根；用于 SKIP / MOVE 的普通文件与大小门禁。
        allow_reuse: 是否允许 SKIP / MOVE；REPAIR / FORCE_FULL 应关闭。

    Returns:
        更新计划。link 文件一律 SKIP；无所有权或磁盘门禁不通过的
        unchanged / moved 文件退化为 PATCH；REMOVE 仅包含受管理路径。
    """
    entries: list[PlanEntry] = []

    if report is None:
        for file in files:
            action = FileAction.SKIP if file.link else FileAction.PATCH
            entries.append(PlanEntry(action=action, path=file.name, file=file))
        return UpdatePlan(entries=entries)

    moved_by_new = {moved.new_path: moved.old_path for moved in report.moved}
    reused_move_sources: set[str] = set()
    status_by_path: dict[str, str] = {}
    for section in (report.unchanged, report.changed, report.added):
        for diff_entry in section:
            status_by_path[diff_entry.path] = diff_entry.status

    for file in files:
        if file.link:
            entries.append(PlanEntry(action=FileAction.SKIP, path=file.name, file=file))
            continue
        if not allow_reuse:
            entries.append(PlanEntry(action=FileAction.PATCH, path=file.name, file=file))
            continue
        move_from = moved_by_new.get(file.name)
        if move_from is not None and move_from in managed_files and _is_regular_size(output_dir, move_from, file.size):
            reused_move_sources.add(move_from)
            entries.append(
                PlanEntry(
                    action=FileAction.MOVE,
                    path=file.name,
                    file=file,
                    move_from=move_from,
                )
            )
            continue

        status = status_by_path.get(file.name)
        if move_from is not None:
            action = FileAction.PATCH
        elif status == "unchanged" and file.name in managed_files and _is_regular_size(output_dir, file.name, file.size):
            action = FileAction.SKIP
        elif status == "added":
            action = FileAction.NEW
        else:
            # changed 或不在报告范围内：交给本地验证兜底。
            action = FileAction.PATCH
        entries.append(PlanEntry(action=action, path=file.name, file=file))

    target_paths = {file.name for file in files}
    for diff_entry in report.removed:
        if diff_entry.path in reused_move_sources or diff_entry.path in target_paths or diff_entry.path not in managed_files:
            continue
        entries.append(PlanEntry(action=FileAction.REMOVE, path=diff_entry.path))

    return UpdatePlan(entries=entries)


def _is_regular_size(output_dir: str | Path | None, path: str, size: int) -> bool:
    """判断受管理路径当前是否为声明大小的普通文件."""
    if output_dir is None:
        return False
    target = Path(output_dir) / Path(path)
    try:
        file_stat = target.lstat()
        return stat.S_ISREG(file_stat.st_mode) and file_stat.st_size == size
    except OSError:
        return False
