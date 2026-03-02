"""Chunk 哈希算法与校验逻辑."""

from __future__ import annotations

import hashlib
import hmac

from riotmanifest.errors import DecompressError

try:
    import blake3
except ImportError:
    blake3 = None

HASH_TYPE_SHA512 = 1
HASH_TYPE_SHA256 = 2
HASH_TYPE_HKDF = 3
HASH_TYPE_BLAKE3 = 4


def hkdf_hash(chunk_data: bytes) -> int:
    """按 RMAN 规则计算 HKDF 派生哈希（uint64）."""
    prk = hashlib.sha256(chunk_data).digest()
    buffer = hmac.new(prk, b"\x00\x00\x00\x01", hashlib.sha256).digest()
    result = int.from_bytes(buffer[:8], "little")
    for _ in range(31):
        buffer = hmac.new(prk, buffer, hashlib.sha256).digest()
        result ^= int.from_bytes(buffer[:8], "little")
    return result


def compute_chunk_hash(chunk_data: bytes, hash_type: int) -> int | None:
    """按 hash_type 计算 chunk 哈希并返回 uint64."""
    if hash_type == HASH_TYPE_SHA256:
        digest = hashlib.sha256(chunk_data).digest()
        return int.from_bytes(digest[:8], "little")
    if hash_type == HASH_TYPE_SHA512:
        digest = hashlib.sha512(chunk_data).digest()
        return int.from_bytes(digest[:8], "little")
    if hash_type == HASH_TYPE_HKDF:
        return hkdf_hash(chunk_data)
    if hash_type == HASH_TYPE_BLAKE3:
        if blake3 is None:
            raise DecompressError("缺少 blake3 依赖，无法校验 Blake3 Chunk 哈希")
        digest = blake3.blake3(chunk_data).digest()
        return int.from_bytes(digest[:8], "little")
    if hash_type == 0:
        return None
    raise DecompressError(f"不支持的 Chunk 哈希类型: {hash_type}")


def validate_chunk_hash(chunk_data: bytes, chunk_id: int, hash_type: int) -> None:
    """校验解压后的 chunk 数据哈希是否与 chunk_id 一致."""
    computed = compute_chunk_hash(chunk_data, hash_type)
    if computed is None:
        return
    if computed != chunk_id:
        raise DecompressError(
            f"Chunk 哈希校验失败: hash_type={hash_type}, computed={computed:016X}, expected={chunk_id:016X}"
        )
