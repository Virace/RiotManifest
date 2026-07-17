"""增量更新（update）子模块导出."""

from riotmanifest.update.verify import (
    ChunkEntry,
    FileVerifyResult,
    iter_chunk_entries,
    verify_file_chunks,
)

__all__ = [
    "ChunkEntry",
    "FileVerifyResult",
    "iter_chunk_entries",
    "verify_file_chunks",
]
