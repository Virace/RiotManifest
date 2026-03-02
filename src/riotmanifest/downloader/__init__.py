"""下载调度模块导出."""

from riotmanifest.downloader.file_pool import FileHandlePool
from riotmanifest.downloader.scheduler import (
    BundleJob,
    ChunkRange,
    DownloadScheduler,
    GlobalChunkTask,
    WriteTarget,
)

__all__ = [
    "FileHandlePool",
    "WriteTarget",
    "GlobalChunkTask",
    "ChunkRange",
    "BundleJob",
    "DownloadScheduler",
]
