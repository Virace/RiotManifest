"""riotmanifest 包导出入口."""

from loguru import logger

from riotmanifest.core.binary_parser import BinaryParser
from riotmanifest.core.errors import DecompressError, DownloadBatchError, DownloadError
from riotmanifest.extractor import WADExtractor
from riotmanifest.game import RiotGameData
from riotmanifest.manifest import PatcherBundle, PatcherChunk, PatcherFile, PatcherManifest
from riotmanifest.utils.http_client import HttpClientError

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
