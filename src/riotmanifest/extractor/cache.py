"""WAD 提取器使用的 Chunk 解压缓存."""

from __future__ import annotations

from collections import OrderedDict
from threading import RLock

CacheKey = tuple[int, int]


class ChunkCache:
    """按 LRU 策略缓存解压后的 chunk 数据."""

    def __init__(self, *, max_bytes: int, max_entries: int):
        """初始化缓存参数。.

        Args:
            max_bytes: 允许缓存的最大字节数，0 表示禁用缓存。
            max_entries: 允许缓存的最大条目数，0 表示禁用缓存。
        """
        self.max_bytes = max(0, max_bytes)
        self.max_entries = max(0, max_entries)
        self._lock = RLock()
        self._entries: OrderedDict[CacheKey, bytes] = OrderedDict()
        self._bytes = 0

    def clear(self) -> None:
        """清空缓存。."""
        with self._lock:
            self._entries.clear()
            self._bytes = 0

    def stats(self) -> dict[str, int]:
        """返回缓存统计信息。."""
        with self._lock:
            return {"entries": len(self._entries), "bytes": self._bytes}

    def get(self, key: CacheKey) -> bytes | None:
        """读取缓存并刷新 LRU 顺序。."""
        with self._lock:
            data = self._entries.get(key)
            if data is None:
                return None
            self._entries.move_to_end(key, last=True)
            return data

    def put(self, key: CacheKey, data: bytes) -> None:
        """写入缓存并执行容量淘汰。."""
        if self.max_entries == 0 or self.max_bytes == 0:
            return

        data_size = len(data)
        if data_size > self.max_bytes:
            return

        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._bytes -= len(old)

            self._entries[key] = data
            self._entries.move_to_end(key, last=True)
            self._bytes += data_size

            while self._entries and (
                len(self._entries) > self.max_entries or self._bytes > self.max_bytes
            ):
                _, removed = self._entries.popitem(last=False)
                self._bytes -= len(removed)
