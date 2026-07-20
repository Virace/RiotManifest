"""staging 临时文件写盘与原子替换工具.

所有下载写入先落到目标同目录的临时文件（staging），
文件数据完整后再原子替换到目标路径；替换前旧文件保持原样，
可作为增量更新时的本地数据复用来源。
"""

from __future__ import annotations

import contextlib
import os
from typing import Union

StrPath = Union[str, "os.PathLike[str]"]

STAGING_SUFFIX = ".rman-tmp"


def staging_path(output: StrPath) -> str:
    """返回目标文件对应的 staging 临时文件路径."""
    return os.fspath(output) + STAGING_SUFFIX


def commit_staging(output: StrPath) -> None:
    """把 staging 文件原子替换到目标路径."""
    os.replace(staging_path(output), os.fspath(output))


def discard_staging(output: StrPath) -> None:
    """删除 staging 文件；不存在时静默."""
    with contextlib.suppress(FileNotFoundError):
        os.remove(staging_path(output))
