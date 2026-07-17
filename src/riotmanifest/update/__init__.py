"""增量更新（update）子模块导出."""

from riotmanifest.update.planner import FileAction, PlanEntry, UpdatePlan, build_update_plan
from riotmanifest.update.result import SyncMode, UpdateResult
from riotmanifest.update.state import InstalledState, ManifestArchive
from riotmanifest.update.updater import ManifestUpdater
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
    "InstalledState",
    "ManifestArchive",
    "FileAction",
    "PlanEntry",
    "UpdatePlan",
    "build_update_plan",
    "SyncMode",
    "UpdateResult",
    "ManifestUpdater",
]
