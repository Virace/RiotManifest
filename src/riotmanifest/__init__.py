"""riotmanifest 包导出入口."""

from loguru import logger

from riotmanifest.core.binary_parser import BinaryParser
from riotmanifest.core.errors import DecompressError, DownloadBatchError, DownloadError
from riotmanifest.diff import (
    ManifestDiffEntry,
    ManifestDiffReport,
    ManifestDiffSummary,
    ManifestMovedEntry,
    WADFileDiffEntry,
    WADHeaderDiffReport,
    WADHeaderDiffSummary,
    WADSectionDiffEntry,
    WADSectionSignature,
    diff_manifests,
    diff_wad_headers,
)
from riotmanifest.downloader import DownloadProgress
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
    "DownloadProgress",
    "WADExtractor",
    "RiotGameData",
    "HttpClientError",
    "ManifestDiffSummary",
    "ManifestDiffEntry",
    "ManifestMovedEntry",
    "ManifestDiffReport",
    "WADSectionSignature",
    "WADSectionDiffEntry",
    "WADFileDiffEntry",
    "WADHeaderDiffSummary",
    "WADHeaderDiffReport",
    "diff_manifests",
    "diff_wad_headers",
]
