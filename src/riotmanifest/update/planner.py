"""diff 报告驱动的更新计划器.

把 `diff_manifests` 的文件级差异报告映射为每文件动作：

- unchanged → SKIP（整个跳过，不验证）
- changed → PATCH（本地验证 + 补洞）
- added → NEW（全新下载）
- moved → MOVE（本地复制，不下载）
- removed → REMOVE（删除旧清单声明的文件）

无旧清单（report=None）时全部按 PATCH 兜底，由本地验证决定实际下载量。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
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
) -> UpdatePlan:
    """把 diff 报告映射为更新计划.

    Args:
        report: `diff_manifests(old, new, include_unchanged=True, detect_moves=True)`
            的报告；None 表示无旧清单。
        files: 新清单侧的目标文件列表（调用方已 filter）。

    Returns:
        更新计划。link 文件一律 SKIP；不在报告范围内的路径按 PATCH 兜底；
        moved 配对的路径不再出现在 NEW / REMOVE 中。
    """
    entries: list[PlanEntry] = []

    if report is None:
        for file in files:
            action = FileAction.SKIP if file.link else FileAction.PATCH
            entries.append(PlanEntry(action=action, path=file.name, file=file))
        return UpdatePlan(entries=entries)

    moved_by_new = {moved.new_path: moved.old_path for moved in report.moved}
    moved_old_paths = set(moved_by_new.values())
    status_by_path: dict[str, str] = {}
    for section in (report.unchanged, report.changed, report.added):
        for diff_entry in section:
            status_by_path[diff_entry.path] = diff_entry.status

    for file in files:
        if file.link:
            entries.append(PlanEntry(action=FileAction.SKIP, path=file.name, file=file))
            continue
        if file.name in moved_by_new:
            entries.append(
                PlanEntry(
                    action=FileAction.MOVE,
                    path=file.name,
                    file=file,
                    move_from=moved_by_new[file.name],
                )
            )
            continue

        status = status_by_path.get(file.name)
        if status == "unchanged":
            action = FileAction.SKIP
        elif status == "added":
            action = FileAction.NEW
        else:
            # changed 或不在报告范围内：交给本地验证兜底。
            action = FileAction.PATCH
        entries.append(PlanEntry(action=action, path=file.name, file=file))

    target_paths = {file.name for file in files}
    for diff_entry in report.removed:
        if diff_entry.path in moved_old_paths or diff_entry.path in target_paths:
            continue
        entries.append(PlanEntry(action=FileAction.REMOVE, path=diff_entry.path))

    return UpdatePlan(entries=entries)
