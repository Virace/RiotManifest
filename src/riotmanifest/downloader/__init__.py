"""下载调度模块导出."""

from riotmanifest.downloader.file_pool import FileHandlePool
from riotmanifest.downloader.scheduler import (
    BundleJob,
    ChunkDownloadResult,
    ChunkEntry,
    ChunkRange,
    DownloadProgress,
    DownloadScheduler,
    GlobalChunkTask,
    ProgressCallback,
    WriteTarget,
    iter_chunk_entries,
)
from riotmanifest.downloader.staging import (
    STAGING_SUFFIX,
    commit_staging,
    discard_staging,
    staging_path,
)

__all__ = [
    "FileHandlePool",
    "WriteTarget",
    "GlobalChunkTask",
    "ChunkDownloadResult",
    "ChunkEntry",
    "iter_chunk_entries",
    "ChunkRange",
    "BundleJob",
    "DownloadScheduler",
    "DownloadProgress",
    "ProgressCallback",
    "STAGING_SUFFIX",
    "staging_path",
    "commit_staging",
    "discard_staging",
]
