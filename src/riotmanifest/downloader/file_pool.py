"""并发下载写盘使用的轻量文件句柄池."""

from __future__ import annotations

import io
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import BinaryIO, Union

StrPath = Union[str, "os.PathLike[str]"]


@dataclass
class _HandleEntry:
    file_obj: BinaryIO
    file_lock: threading.Lock
    refs: int = 0
    evicted: bool = False


class FileHandlePool:
    """轻量文件句柄池，避免每次写入都重复 open/close。."""

    def __init__(self, max_handles: int = 500):
        """初始化句柄池.

        Args:
            max_handles: 池内最多缓存的文件句柄数量。
        """
        self.max_handles = max(1, max_handles)
        self._handles: OrderedDict[str, _HandleEntry] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _close_entry(entry: _HandleEntry):
        with entry.file_lock:
            entry.file_obj.close()

    def _evict_one_locked(self) -> list[_HandleEntry]:
        if not self._handles:
            return []

        _, entry = self._handles.popitem(last=False)
        entry.evicted = True
        if entry.refs == 0:
            return [entry]
        return []

    def _acquire(self, path: StrPath) -> _HandleEntry:
        norm_path = os.fspath(path)
        to_close: list[_HandleEntry] = []
        entry: _HandleEntry | None = None
        try:
            with self._lock:
                if norm_path in self._handles:
                    entry = self._handles.pop(norm_path)
                    entry.refs += 1
                    self._handles[norm_path] = entry
                    return entry

                while len(self._handles) >= self.max_handles:
                    to_close.extend(self._evict_one_locked())

                file_obj = io.FileIO(norm_path, mode="r+")
                entry = _HandleEntry(file_obj=file_obj, file_lock=threading.Lock(), refs=1)
                self._handles[norm_path] = entry
                return entry
        finally:
            for close_entry in to_close:
                self._close_entry(close_entry)

    def _release(self, entry: _HandleEntry):
        should_close = False
        with self._lock:
            entry.refs -= 1
            should_close = entry.refs == 0 and entry.evicted
        if should_close:
            self._close_entry(entry)

    def write_at(self, path: StrPath, data: bytes, offset: int):
        """向目标文件指定偏移写入字节数据."""
        entry = self._acquire(path)
        try:
            with entry.file_lock:
                entry.file_obj.seek(offset)
                entry.file_obj.write(data)
        finally:
            self._release(entry)

    def close(self):
        """关闭并清空池内所有句柄."""
        to_close: list[_HandleEntry] = []
        with self._lock:
            handles = list(self._handles.values())
            self._handles.clear()
            for entry in handles:
                entry.evicted = True
                if entry.refs == 0:
                    to_close.append(entry)

        for entry in to_close:
            self._close_entry(entry)
