import asyncio
import os
import tempfile
import time
from typing import List, Tuple

import pytest

from riotmanifest.game import RiotGameData
from riotmanifest.http_client import HttpClientError
from riotmanifest.manifest import PatcherFile, PatcherManifest


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    return int(value)


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    return float(value)


def _pick_files(
    files: List[PatcherFile],
    suffix: str,
    target_bytes: int,
    max_files: int,
    min_file_bytes: int,
    max_file_bytes: int,
    prefer_many_files: bool,
) -> Tuple[List[PatcherFile], int]:
    suffix = suffix.lower()

    # 优先压测 .wad.client 文件
    candidates = [
        f
        for f in files
        if not f.link
        and f.name.lower().endswith(suffix)
        and min_file_bytes <= f.size <= max_file_bytes
    ]

    # prefer_many_files=True 时优先小文件以增加样本文件数；
    # 否则优先大文件以更快达到目标流量。
    candidates.sort(key=lambda f: f.size, reverse=not prefer_many_files)

    selected: List[PatcherFile] = []
    total = 0
    for file in candidates:
        if len(selected) >= max_files:
            break
        selected.append(file)
        total += file.size
        if total >= target_bytes:
            break

    # 如果严格过滤不足，放宽大小范围但仍保持后缀
    if total < target_bytes:
        relaxed = [
            f for f in files if not f.link and f.name.lower().endswith(suffix) and f not in selected and f.size > 0
        ]
        relaxed.sort(key=lambda f: f.size, reverse=not prefer_many_files)
        for file in relaxed:
            if len(selected) >= max_files:
                break
            selected.append(file)
            total += file.size
            if total >= target_bytes:
                break

    return selected, total


def _load_latest_game_with_retry(region: str, retries: int, retry_delay_sec: float) -> dict:
    last_error = None
    for attempt in range(retries):
        try:
            rgd = RiotGameData()
            rgd.load_game_data(regions=[region])
            latest = rgd.latest_game(region)
            if latest:
                return latest
            last_error = RuntimeError(f"未获取到区域 {region} 的最新 GAME 清单")
        except HttpClientError as exc:
            last_error = exc

        if attempt + 1 < retries:
            time.sleep(retry_delay_sec * (attempt + 1))

    raise RuntimeError(f"获取最新 GAME 清单失败，重试 {retries} 次后仍失败: {last_error}")


@pytest.mark.integration
def test_game_manifest_overall_download_speed():
    """
    真实清单压力测速（网络集成测试，pytest 版本）。

    运行开关：
      RIOT_PERF_RUN=1

    推荐高压参数（示例）：
      RIOT_PERF_RUN=1
      RIOT_PERF_TARGET_MB=1024
      RIOT_PERF_MAX_FILES=1200
      RIOT_PERF_CONCURRENCY=16
    """
    if os.getenv("RIOT_PERF_RUN", "0") != "1":
        pytest.skip("未启用压力测试（设置 RIOT_PERF_RUN=1 可执行）")

    region = os.getenv("RIOT_PERF_REGION", "EUW1")
    suffix = os.getenv("RIOT_PERF_SUFFIX", ".wad.client")
    target_mb = _env_int("RIOT_PERF_TARGET_MB", 512)
    max_files = _env_int("RIOT_PERF_MAX_FILES", 1200)
    min_file_kb = _env_int("RIOT_PERF_MIN_FILE_KB", 256)
    max_file_mb = _env_int("RIOT_PERF_MAX_FILE_MB", 64)
    concurrency = _env_int("RIOT_PERF_CONCURRENCY", 16)
    min_mbps = _env_float("RIOT_PERF_MIN_MBPS", 0.0)
    min_elapsed_sec = _env_float("RIOT_PERF_MIN_ELAPSED_SEC", 3.0)
    min_files = _env_int("RIOT_PERF_MIN_FILES", 10)
    meta_retries = _env_int("RIOT_PERF_META_RETRIES", 5)
    meta_retry_delay_sec = _env_float("RIOT_PERF_META_RETRY_DELAY_SEC", 2.0)
    pick_mode = os.getenv("RIOT_PERF_PICK_MODE", "many").strip().lower()
    prefer_many_files = pick_mode != "large"

    target_bytes = target_mb * 1024 * 1024
    min_file_bytes = min_file_kb * 1024
    max_file_bytes = max_file_mb * 1024 * 1024

    latest = _load_latest_game_with_retry(
        region=region,
        retries=meta_retries,
        retry_delay_sec=meta_retry_delay_sec,
    )

    manifest_url = latest["url"]
    game_version = latest["version"]

    with tempfile.TemporaryDirectory(prefix="riotmanifest_perf_") as out_dir:
        manifest = PatcherManifest(
            file=manifest_url,
            path=out_dir,
            concurrency_limit=concurrency,
        )

        # 优先使用 README 推荐的语言+后缀过滤方式进行样本挑选
        filtered = list(manifest.filter_files(flag="zh_CN", pattern="wad.client"))
        candidate_files = filtered if filtered else list(manifest.files.values())

        selected, planned_bytes = _pick_files(
            candidate_files,
            suffix=suffix,
            target_bytes=target_bytes,
            max_files=max_files,
            min_file_bytes=min_file_bytes,
            max_file_bytes=max_file_bytes,
            prefer_many_files=prefer_many_files,
        )

        assert selected, f"未筛选出可下载文件（suffix={suffix}）"
        assert len(selected) >= min_files, f"筛选文件数不足: {len(selected)} < {min_files}"
        assert planned_bytes > 0, "计划下载字节数为 0"

        start = time.perf_counter()
        results = asyncio.run(
            manifest.download_files_concurrently(
                selected,
                concurrency_limit=concurrency,
            )
        )
        elapsed = time.perf_counter() - start

        assert len(results) == len(selected), "返回结果数量与待下载文件数量不一致"
        assert all(results), "存在下载失败文件"

        downloaded_bytes = 0
        for file in selected:
            output = os.path.join(out_dir, file.name)
            assert os.path.isfile(output), f"文件不存在: {file.name}"
            size = os.path.getsize(output)
            assert size == file.size, f"文件大小不匹配: {file.name}"
            downloaded_bytes += size

        downloaded_mb = downloaded_bytes / (1024 * 1024)
        planned_mb = planned_bytes / (1024 * 1024)
        mbps = downloaded_mb / max(elapsed, 1e-9)

        jobs = manifest._build_bundle_jobs(selected)
        unique_bundles = len({job.bundle_id for job in jobs})
        total_ranges = sum(len(job.ranges) for job in jobs)
        unique_chunks = sum(len(tasks) for tasks in manifest._build_global_task_map(selected).values())

        print(
                "\n[PERF] "
                f"region={region} version={game_version} suffix={suffix}\n"
                f"[PERF] manifest={manifest_url}\n"
                f"[PERF] files={len(selected)} planned={planned_mb:.2f}MB downloaded={downloaded_mb:.2f}MB "
                f"elapsed={elapsed:.3f}s throughput={mbps:.2f}MB/s concurrency={concurrency} "
                f"pick_mode={pick_mode} candidates={len(candidate_files)} filtered_zh_cn={len(filtered)}\n"
                f"[PERF] jobs={len(jobs)} unique_bundles={unique_bundles} ranges={total_ranges} "
                f"unique_chunks={unique_chunks}"
            )

        assert mbps > 0.0, "测速结果无效，吞吐率为 0"
        assert mbps >= min_mbps, f"吞吐率低于阈值: {mbps:.2f}MB/s < {min_mbps:.2f}MB/s"
        assert elapsed >= min_elapsed_sec, (
            f"压测耗时不足: {elapsed:.3f}s < {min_elapsed_sec:.3f}s，"
            f"可提高 RIOT_PERF_TARGET_MB 或降低 RIOT_PERF_MAX_FILE_MB"
        )
