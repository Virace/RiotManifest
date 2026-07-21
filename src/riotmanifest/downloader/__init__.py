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
    JobGroup,
    ProgressCallback,
    WriteTarget,
    iter_chunk_entries,
    run_job_groups,
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
    "JobGroup",
    "run_job_groups",
    "DownloadScheduler",
    "DownloadProgress",
    "ProgressCallback",
    "STAGING_SUFFIX",
    "staging_path",
    "commit_staging",
    "discard_staging",
]
