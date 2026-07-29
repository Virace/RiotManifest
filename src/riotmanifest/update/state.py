"""清单存档与 installed.json 本地状态管理.

目录布局（位于下载输出目录内）::

    <output>/.rman/installed.json              当前安装状态指针
    <output>/.rman/manifests/<ID>.manifest     清单原文存档（保留当前 + 上一份）

installed.json 的 schema 与 RiotManifestGo 项目共享，修改必须双侧同步。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Union

StrPath = Union[str, "os.PathLike[str]"]

STATE_DIR_NAME = ".rman"
MANIFEST_DIR_NAME = "manifests"
INSTALLED_FILE_NAME = "installed.json"
LEGACY_STATE_SCHEMA = 1
STATE_SCHEMA = 2


def _normalize_path(path: str) -> str | None:
    """把状态路径规范为安全的 manifest 相对路径；无效路径返回 None."""
    raw = path.replace("\\", "/")
    parts = raw.split("/")
    if not raw or any(not part or part in {".", ".."} or ":" in part for part in parts):
        return None
    return "/".join(parts)


def _normalize_files(files: Iterable[str]) -> list[str] | None:
    """规范化、去重并排序状态文件集合；任一成员无效时返回 None."""
    if isinstance(files, (str, bytes)):
        return None
    normalized: set[str] = set()
    for path in files:
        if not isinstance(path, str):
            return None
        clean = _normalize_path(path)
        if clean is None:
            return None
        normalized.add(clean)
    return sorted(normalized)


@dataclass(slots=True)
class InstalledState:
    """installed.json 内容；`files` 是当前清单下已确认的受管理覆盖."""

    schema: int
    manifest_id: str
    manifest_file: str
    source: str
    updated_at: str
    files: list[str] = field(default_factory=list)


class ManifestArchive:
    """输出目录内 `.rman/` 下的清单存档与安装状态."""

    def __init__(self, output_dir: StrPath):
        """初始化存档对象.

        Args:
            output_dir: 下载输出目录（`.rman/` 会建在其内）。
        """
        self.root = Path(output_dir) / STATE_DIR_NAME

    @property
    def installed_file(self) -> Path:
        """installed.json 的完整路径."""
        return self.root / INSTALLED_FILE_NAME

    def load_installed(self) -> InstalledState | None:
        """读取安装状态；schema 1 仅保留清单指针，不授予文件所有权."""
        try:
            payload = json.loads(self.installed_file.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        schema = payload.get("schema")
        if not isinstance(schema, int) or isinstance(schema, bool):
            return None
        if schema not in {LEGACY_STATE_SCHEMA, STATE_SCHEMA}:
            return None
        try:
            files = [] if schema == LEGACY_STATE_SCHEMA else _normalize_files(payload["files"])
            manifest_file = payload["manifest_file"]
            if not isinstance(manifest_file, str):
                return None
            manifest_file = _normalize_path(manifest_file)
            if files is None or manifest_file is None:
                return None
            return InstalledState(
                schema=schema,
                manifest_id=str(payload["manifest_id"]),
                manifest_file=manifest_file,
                source=str(payload.get("source", "")),
                updated_at=str(payload.get("updated_at", "")),
                files=files,
            )
        except (KeyError, TypeError):
            return None

    def installed_manifest_path(self) -> Path | None:
        """返回安装状态指向的清单存档路径；状态缺失或文件不存在时返回 None."""
        state = self.load_installed()
        if state is None:
            return None
        manifest_path = self.root / state.manifest_file
        return manifest_path if manifest_path.is_file() else None

    def save(
        self,
        manifest_id: int,
        raw: bytes,
        source: str,
        files: Iterable[str] = (),
    ) -> None:
        """存档清单原文并原子更新 installed.json.

        清单存档只保留本次与上一份（此前 installed.json 指向的），更旧的清理。

        Args:
            manifest_id: 清单 ID（uint64）。
            raw: 清单原始字节。
            source: 用户输入的清单 URL 或本地路径（仅溯源用）。
            files: 已确认匹配该清单的受管理文件路径。

        Raises:
            ValueError: `files` 含非 manifest 相对路径。
        """
        normalized_files = _normalize_files(files)
        if normalized_files is None:
            raise ValueError("files 必须全部是有效的 manifest 相对路径")
        previous = self.load_installed()
        manifest_dir = self.root / MANIFEST_DIR_NAME
        manifest_dir.mkdir(parents=True, exist_ok=True)

        manifest_name = f"{manifest_id:016X}.manifest"
        (manifest_dir / manifest_name).write_bytes(raw)

        state = InstalledState(
            schema=STATE_SCHEMA,
            manifest_id=f"{manifest_id:016X}",
            manifest_file=f"{MANIFEST_DIR_NAME}/{manifest_name}",
            source=source,
            # datetime.UTC 需 3.11+，项目下限为 3.10。
            updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),  # noqa: UP017
            files=normalized_files,
        )
        tmp_file = self.installed_file.with_name(INSTALLED_FILE_NAME + ".tmp")
        tmp_file.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp_file, self.installed_file)

        keep = {manifest_name}
        if previous is not None:
            keep.add(Path(previous.manifest_file).name)
        for stale in manifest_dir.glob("*.manifest"):
            if stale.name not in keep:
                stale.unlink(missing_ok=True)
