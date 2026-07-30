"""update 编排的模式与结果模型."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from riotmanifest.core.errors import BundleJobFailure
    from riotmanifest.update.planner import FileAction


class SyncMode(Enum):
    """同步模式.

    - AUTO：默认。逐文件验证补洞，有旧清单（显式或存档）时自动跳过未变化文件。
      注意：被跳过的文件不做本地验证（rman `--update` 同款权衡），
      怀疑本地损坏时请用 REPAIR。
    - REPAIR：不做文件级跳过，对全部目标文件逐 chunk 验证并补洞（修复）。
    - VERIFY_ONLY：不做文件级跳过，只验证并报告缺失量，
      不写盘、不下载、不存档（dry-run）。
    - FORCE_FULL：跳过一切验证，强制全量重下。
    """

    AUTO = "auto"
    REPAIR = "repair"
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
        failures: bundle 维度的下载失败详情（文件视角见 `failed`）。
            `error` 为重试耗尽后的包装异常，底层原因沿 `__cause__` 链保留。
        verify_only: 本次是否为 VERIFY_ONLY 运行。
        committed_files: 收尾阶段经 staging 成功提交到目标路径的清单文件名。
            只统计本轮真实写盘的文件；完全命中的 PATCH、REMOVE 和下载失败文件
            不计入。顺序与计划内文件顺序一致，同一路径最多出现一次。
            VERIFY_ONLY 恒为空列表。`actions` 仍保留计划口径。
    """

    actions: dict[str, FileAction] = field(default_factory=dict)
    downloaded_bytes: int = 0
    reused_bytes: int = 0
    missing_bytes: int = 0
    removed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    failures: list[BundleJobFailure] = field(default_factory=list)
    verify_only: bool = False
    committed_files: list[str] = field(default_factory=list)
