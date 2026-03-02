"""riotmanifest 包导出入口."""

from loguru import logger

from riotmanifest.extractor import WADExtractor
from riotmanifest.game import RiotGameData
from riotmanifest.http_client import HttpClientError
from riotmanifest.manifest import (
    BinaryParser,
    DecompressError,
    DownloadBatchError,
    DownloadError,
    PatcherBundle,
    PatcherChunk,
    PatcherFile,
    PatcherManifest,
)

logger.disable("riotmanifest")

__all__ = [
    "DownloadError",
    "DownloadBatchError",
    "DecompressError",
    "BinaryParser",
    "PatcherChunk",
    "PatcherBundle",
    "PatcherFile",
    "PatcherManifest",
    "WADExtractor",
    "RiotGameData",
    "HttpClientError",
]
