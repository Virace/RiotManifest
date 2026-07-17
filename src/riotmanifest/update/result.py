"""update 编排的模式与结果模型."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from riotmanifest.update.planner import FileAction


class SyncMode(Enum):
    """同步模式.

    - AUTO：默认。存在即验证补洞，有旧清单时自动跳过未变化文件。
    - VERIFY_ONLY：只验证并报告，不写盘、不下载、不存档（dry-run）。
    - FORCE_FULL：跳过一切验证，强制全量重下。
    """

    AUTO = "auto"
    VERIFY_ONLY = "verify_only"
    FORCE_FULL = "force_full"


@dataclass(slots=True)
class UpdateResult:
    """一次 sync 的结构化结果.

    Attributes:
        actions: path → 计划动作（含 REMOVE 条目）。
        downloaded_bytes: 网络补洞写入的解压域字节数（扇出写按目标各计一次）。
        reused_bytes: 本地复用/验证命中的解压域字节数（PATCH 命中 + MOVE 复制）。
        missing_bytes: 未满足的解压域字节数（VERIFY_ONLY 的缺失量，或下载失败文件的缺口）。
        removed: 实际删除的 REMOVE 路径列表。
        failed: 下载失败的文件路径列表（其旧文件保持原样）。
        verify_only: 本次是否为 VERIFY_ONLY 运行。
    """

    actions: dict[str, FileAction] = field(default_factory=dict)
    downloaded_bytes: int = 0
    reused_bytes: int = 0
    missing_bytes: int = 0
    removed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    verify_only: bool = False
