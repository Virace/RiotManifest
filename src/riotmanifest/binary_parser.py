"""二进制流读取辅助工具."""

from __future__ import annotations

import struct
from typing import BinaryIO


class BinaryParser:
    """按结构定义读取二进制流的简易解析器。."""

    def __init__(self, f: BinaryIO):
        """初始化二进制读取器.

        Args:
            f: 待读取的二进制流对象。
        """
        self.f = f

    def tell(self):
        """返回当前读取偏移."""
        return self.f.tell()

    def seek(self, position: int):
        """跳转到绝对偏移."""
        self.f.seek(position, 0)

    def skip(self, amount: int):
        """相对向前跳过字节数."""
        self.f.seek(amount, 1)

    def rewind(self, amount: int):
        """相对向后回退字节数."""
        self.f.seek(-amount, 1)

    def unpack(self, fmt: str):
        """按 struct 格式读取并解包."""
        length = struct.calcsize(fmt)
        return struct.unpack(fmt, self.f.read(length))

    def raw(self, length: int):
        """读取指定长度原始字节."""
        return self.f.read(length)

    def unpack_string(self):
        """读取并解包长度前缀字符串."""
        return self.f.read(self.unpack("<L")[0]).decode("utf-8")
