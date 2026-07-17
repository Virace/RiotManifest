"""chunk 级本地文件固定位置验证.

按新清单的 chunk 布局在本地文件对应偏移读取、哈希并与 chunk_id 比对：
命中的块可直接复用，miss 的块交给下载补洞。不做滑动窗口搜索。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Union

from riotmanifest.core.chunk_hash import compute_chunk_hash
from riotmanifest.downloader.scheduler import ChunkEntry, iter_chunk_entries

if TYPE_CHECKING:
    from riotmanifest.manifest import PatcherFile

StrPath = Union[str, "os.PathLike[str]"]

__all__ = [
    "ChunkEntry",
    "FileVerifyResult",
    "iter_chunk_entries",
    "verify_file_chunks",
]


@dataclass(slots=True)
class FileVerifyResult:
    """单文件本地验证结果."""

    file: PatcherFile
    exists: bool
    hits: list[ChunkEntry]
    misses: list[ChunkEntry]

    @property
    def complete(self) -> bool:
        """本地文件是否已与清单布局完全一致."""
        return self.exists and not self.misses

    @property
    def reused_bytes(self) -> int:
        """命中块的解压域字节数合计."""
        return sum(entry.chunk.target_size for entry in self.hits)


def verify_file_chunks(file: PatcherFile, path: StrPath) -> FileVerifyResult:
    """对本地文件按新清单布局做固定位置逐块验证.

    文件不存在 → 全部 miss；hash_type 未知（0）或区间越界 → miss；
    同一 chunk_id 出现在多个位置时按位置独立判定。

    Args:
        file: 新清单中的目标文件。
        path: 本地文件路径（通常为该文件的目标输出路径）。

    Returns:
        命中与 miss 的 chunk 条目列表。
    """
    entries = list(iter_chunk_entries(file))
    if not os.path.isfile(path):
        return FileVerifyResult(file=file, exists=False, hits=[], misses=entries)

    hits: list[ChunkEntry] = []
    misses: list[ChunkEntry] = []
    local_size = os.path.getsize(path)
    with open(path, "rb") as f:
        for entry in entries:
            chunk = entry.chunk
            hash_type = file.chunk_hash_types.get(chunk.chunk_id, 0)
            if hash_type == 0 or entry.file_offset + chunk.target_size > local_size:
                misses.append(entry)
                continue
            f.seek(entry.file_offset)
            data = f.read(chunk.target_size)
            if len(data) == chunk.target_size and compute_chunk_hash(data, hash_type) == chunk.chunk_id:
                hits.append(entry)
            else:
                misses.append(entry)
    return FileVerifyResult(file=file, exists=True, hits=hits, misses=misses)
