#!/usr/bin/env python3
"""对 Riot CDN 域名做 IP 收集、直连探测、总决赛选优与 hosts 写入."""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import ssl
from collections import defaultdict
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from threading import local
from time import perf_counter
from urllib.parse import quote_plus, urlsplit
from urllib.request import Request, urlopen

import urllib3
from urllib3 import exceptions as urllib3_exceptions
from urllib3.util import Timeout

DEFAULT_DOMAINS = (
    "lol.dyn.riotcdn.net",
    "lol.secure.dyn.riotcdn.net",
)
DEFAULT_DOH_SOURCES: tuple[tuple[str, str, Mapping[str, str]], ...] = (
    (
        "google",
        "https://dns.google/resolve?name={domain}&type=A",
        {},
    ),
    (
        "cloudflare",
        "https://cloudflare-dns.com/dns-query?name={domain}&type=A",
        {"accept": "application/dns-json"},
    ),
)
DEFAULT_OUTPUT_JSON = Path("out") / "cdn_ip_probe_report.json"
DEFAULT_DOWNLOAD_PATH = "channels/public/releases/26E55582156E257F.manifest"
DEFAULT_HOSTS_FILE = Path("/etc/hosts")
DEFAULT_HOSTS_BACKUP_SUFFIX = ".riotmanifest.bak"
USER_AGENT = "riot-cdn-ip-probe/3.0"
_CHUNK_SIZE = 64 * 1024
_SSL_CONTEXT_LOCAL = local()
_HOSTS_BLOCK_BEGIN = "# >>> riotmanifest-cdn begin >>>"
_HOSTS_BLOCK_END = "# <<< riotmanifest-cdn end <<<"
_HOSTS_MANAGED_COMMENT = "# managed-by:select_cdn_ip.py"


@dataclass(frozen=True)
class IpResolveRecord:
    """单个域名解析结果."""

    domain: str
    source: str
    ips: tuple[str, ...]
    error: str | None


@dataclass(frozen=True)
class DownloadSample:
    """单次 urllib3 直连下载样本."""

    ok: bool
    status: int | None
    elapsed_ms: float | None
    bytes_read: int | None
    throughput_mbps: float | None
    error: str | None


@dataclass(frozen=True)
class ProbeSample:
    """单次连通+下载探测样本."""

    ok: bool
    tcp_ms: float | None
    tls_ms: float | None
    ttfb_ms: float | None
    status_line: str | None
    error: str | None
    download_ok: bool | None
    download_status: int | None
    download_ms: float | None
    download_bytes: int | None
    download_mbps: float | None
    download_error: str | None


@dataclass(frozen=True)
class DomainIpProbeResult:
    """单域名单 IP 聚合探测结果."""

    domain: str
    ip: str
    attempts: int
    success_count: int
    success_rate: float
    download_attempts: int
    download_success_count: int
    download_success_rate: float | None
    median_tcp_ms: float | None
    median_tls_ms: float | None
    median_ttfb_ms: float | None
    median_download_ms: float | None
    median_download_bytes: float | None
    median_download_mbps: float | None
    sample_status_lines: tuple[str, ...]
    sample_download_statuses: tuple[int, ...]
    sample_errors: tuple[str, ...]


def _build_argument_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器."""
    parser = argparse.ArgumentParser(
        description="收集 Riot CDN 域名可用 IP，并基于真实请求优选。",
    )
    parser.add_argument(
        "--domain",
        action="append",
        default=[],
        help="目标域名，可重复传参；不传则默认同时测试两个 Riot CDN 域名。",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=3,
        help="每个 IP 探测次数（默认 3）。",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="单次 TCP/TLS/下载超时时间（秒，默认 3）。",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=24,
        help="并发探测 worker 数（默认 24）。",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="每个域名打印前 N 个最优 IP（默认 10）。",
    )
    parser.add_argument(
        "--probe-path",
        default="",
        help="连通探测 HTTP 路径；不传时优先复用 --download-path。",
    )
    parser.add_argument(
        "--download-path",
        default=DEFAULT_DOWNLOAD_PATH,
        help=(
            "真实下载测试路径（相对路径或完整 URL）。"
            "默认 channels/public/releases/26E55582156E257F.manifest；"
            "传空字符串可关闭下载探测。"
        ),
    )
    parser.add_argument(
        "--download-max-bytes",
        type=int,
        default=0,
        help="下载探测最大读取字节数（0 表示读完整响应，默认 0）。",
    )
    parser.add_argument(
        "--skip-system-resolver",
        action="store_true",
        help="跳过系统 DNS，仅使用 DoH 源收集 IP。",
    )
    parser.add_argument(
        "--apply-hosts",
        action="store_true",
        help="把每个 CDN 的优选 IP 写入 hosts。",
    )
    parser.add_argument(
        "--hosts-file",
        type=Path,
        default=DEFAULT_HOSTS_FILE,
        help="hosts 文件路径（默认 /etc/hosts）。",
    )
    parser.add_argument(
        "--hosts-backup",
        dest="hosts_backup",
        action="store_true",
        default=True,
        help="写入 hosts 前先备份（默认开启）。",
    )
    parser.add_argument(
        "--no-hosts-backup",
        dest="hosts_backup",
        action="store_false",
        help="写入 hosts 时不生成备份。",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=DEFAULT_OUTPUT_JSON,
        help="输出 JSON 报告路径。",
    )
    return parser


def _normalize_domains(raw_domains: Iterable[str]) -> tuple[str, ...]:
    """标准化并去重域名列表."""
    deduplicated: dict[str, str] = {}
    for domain in raw_domains:
        cleaned = domain.strip().lower().rstrip(".")
        if not cleaned:
            continue
        deduplicated.setdefault(cleaned, cleaned)
    if deduplicated:
        return tuple(deduplicated.values())
    return DEFAULT_DOMAINS


def _normalize_request_path(raw_path: str) -> str:
    """标准化请求路径，支持传完整 URL."""
    candidate = raw_path.strip()
    if not candidate:
        return "/"
    if candidate.startswith(("http://", "https://")):
        parsed = urlsplit(candidate)
        normalized = parsed.path or "/"
        if parsed.query:
            normalized = f"{normalized}?{parsed.query}"
        return normalized
    if not candidate.startswith("/"):
        return f"/{candidate}"
    return candidate


def _normalize_optional_request_path(raw_path: str) -> str | None:
    """标准化可选请求路径；空值表示禁用该探测."""
    if not raw_path.strip():
        return None
    return _normalize_request_path(raw_path)


def _extract_ipv4_from_answers(answer_items: Iterable[object]) -> tuple[str, ...]:
    """从 DoH Answer 字段中提取 IPv4 记录."""
    ip_list: list[str] = []
    seen: set[str] = set()
    for item in answer_items:
        if not isinstance(item, Mapping):
            continue
        if int(item.get("type", 0)) != 1:
            continue
        data = item.get("data")
        if not isinstance(data, str):
            continue
        ip = data.strip()
        try:
            parsed = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if parsed.version != 4:
            continue
        if ip in seen:
            continue
        seen.add(ip)
        ip_list.append(ip)
    return tuple(ip_list)


def _resolve_a_records_via_doh(
    *,
    domain: str,
    source_name: str,
    url_template: str,
    headers: Mapping[str, str],
    timeout: float,
) -> IpResolveRecord:
    """使用单个 DoH 源解析域名 A 记录."""
    endpoint = url_template.format(domain=quote_plus(domain))
    request = Request(endpoint, headers=dict(headers))
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        return IpResolveRecord(
            domain=domain,
            source=source_name,
            ips=tuple(),
            error=str(exc),
        )
    answers = payload.get("Answer", [])
    ips = _extract_ipv4_from_answers(answers if isinstance(answers, list) else [])
    return IpResolveRecord(
        domain=domain,
        source=source_name,
        ips=ips,
        error=None,
    )


def _resolve_a_records_via_system(domain: str) -> IpResolveRecord:
    """使用系统 DNS 解析域名 A 记录."""
    ips: list[str] = []
    seen: set[str] = set()
    try:
        records = socket.getaddrinfo(
            domain,
            443,
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
        )
    except Exception as exc:  # noqa: BLE001
        return IpResolveRecord(
            domain=domain,
            source="system",
            ips=tuple(),
            error=str(exc),
        )
    for item in records:
        ip = item[4][0]
        if ip in seen:
            continue
        seen.add(ip)
        ips.append(ip)
    return IpResolveRecord(
        domain=domain,
        source="system",
        ips=tuple(ips),
        error=None,
    )


def _collect_candidate_ips(
    *,
    domains: tuple[str, ...],
    timeout: float,
    skip_system_resolver: bool,
) -> tuple[list[IpResolveRecord], dict[str, tuple[str, ...]]]:
    """并发收集域名候选 IP."""
    records: list[IpResolveRecord] = []
    domain_ips: dict[str, set[str]] = {domain: set() for domain in domains}
    futures = []

    with ThreadPoolExecutor(
        max_workers=max(4, len(domains) * 3),
        thread_name_prefix="dns-probe",
    ) as executor:
        for domain in domains:
            if not skip_system_resolver:
                futures.append(executor.submit(_resolve_a_records_via_system, domain))
            for source_name, url_template, headers in DEFAULT_DOH_SOURCES:
                futures.append(
                    executor.submit(
                        _resolve_a_records_via_doh,
                        domain=domain,
                        source_name=source_name,
                        url_template=url_template,
                        headers=headers,
                        timeout=timeout,
                    )
                )

        for future in as_completed(futures):
            record = future.result()
            records.append(record)
            domain_ips[record.domain].update(record.ips)

    resolved = {
        domain: tuple(
            sorted(
                domain_ips[domain],
                key=lambda ip: tuple(int(part) for part in ip.split(".")),
            )
        )
        for domain in domains
    }
    return sorted(records, key=lambda item: (item.domain, item.source)), resolved


def _get_thread_ssl_context() -> ssl.SSLContext:
    """获取线程级复用 SSLContext，减少重复构造开销."""
    context = getattr(_SSL_CONTEXT_LOCAL, "context", None)
    if isinstance(context, ssl.SSLContext):
        return context
    context = ssl.create_default_context()
    _SSL_CONTEXT_LOCAL.context = context
    return context


def _probe_https_once(*, domain: str, ip: str, request_path: str, timeout: float) -> ProbeSample:
    """对单个 IP 执行一次 TCP/TLS/HTTP 头探测."""
    context = _get_thread_ssl_context()
    tcp_start = perf_counter()
    try:
        with socket.create_connection((ip, 443), timeout=timeout) as sock:
            tcp_ms = (perf_counter() - tcp_start) * 1000
            tls_start = perf_counter()
            with context.wrap_socket(sock, server_hostname=domain) as tls_sock:
                tls_ms = (perf_counter() - tls_start) * 1000
                tls_sock.settimeout(timeout)
                request_start = perf_counter()
                request_payload = (
                    f"HEAD {request_path} HTTP/1.1\r\nHost: {domain}\r\nConnection: close\r\nUser-Agent: {USER_AGENT}\r\n\r\n"
                ).encode("ascii")
                tls_sock.sendall(request_payload)
                first_byte = tls_sock.recv(1)
                if not first_byte:
                    return ProbeSample(
                        ok=False,
                        tcp_ms=round(tcp_ms, 3),
                        tls_ms=round(tls_ms, 3),
                        ttfb_ms=None,
                        status_line=None,
                        error="empty response",
                        download_ok=None,
                        download_status=None,
                        download_ms=None,
                        download_bytes=None,
                        download_mbps=None,
                        download_error=None,
                    )
                ttfb_ms = (perf_counter() - request_start) * 1000
                buffered = first_byte + tls_sock.recv(4096)
                status_line = buffered.split(b"\r\n", 1)[0].decode(
                    "utf-8",
                    errors="replace",
                )
                return ProbeSample(
                    ok=True,
                    tcp_ms=round(tcp_ms, 3),
                    tls_ms=round(tls_ms, 3),
                    ttfb_ms=round(ttfb_ms, 3),
                    status_line=status_line,
                    error=None,
                    download_ok=None,
                    download_status=None,
                    download_ms=None,
                    download_bytes=None,
                    download_mbps=None,
                    download_error=None,
                )
    except Exception as exc:  # noqa: BLE001
        return ProbeSample(
            ok=False,
            tcp_ms=None,
            tls_ms=None,
            ttfb_ms=None,
            status_line=None,
            error=str(exc),
            download_ok=None,
            download_status=None,
            download_ms=None,
            download_bytes=None,
            download_mbps=None,
            download_error=None,
        )


def _build_direct_ip_pool(*, domain: str, ip: str, timeout: float) -> urllib3.HTTPSConnectionPool:
    """创建“域名语义 + 指定 IP 直连”的 urllib3 连接池."""
    return urllib3.HTTPSConnectionPool(
        host=ip,
        port=443,
        timeout=Timeout(connect=timeout, read=timeout),
        retries=False,
        maxsize=1,
        block=False,
        assert_hostname=domain,
        server_hostname=domain,
    )


def _probe_download_once(
    *,
    pool: urllib3.HTTPSConnectionPool,
    domain: str,
    request_path: str,
    download_max_bytes: int,
) -> DownloadSample:
    """基于 urllib3 执行一次真实下载探测."""
    response: urllib3.response.HTTPResponse | None = None
    start = perf_counter()
    try:
        response = pool.request(
            "GET",
            request_path,
            headers={
                "Host": domain,
                "Connection": "close",
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
            },
            preload_content=False,
        )
        status = int(response.status)
        if status >= 400:
            return DownloadSample(
                ok=False,
                status=status,
                elapsed_ms=None,
                bytes_read=None,
                throughput_mbps=None,
                error=f"HTTP {status}",
            )

        bytes_read = 0
        remaining = download_max_bytes
        while True:
            chunk_limit = _CHUNK_SIZE
            if download_max_bytes > 0:
                chunk_limit = min(chunk_limit, remaining)
                if chunk_limit <= 0:
                    break
            chunk = response.read(chunk_limit)
            if not chunk:
                break
            bytes_read += len(chunk)
            if download_max_bytes > 0:
                remaining -= len(chunk)
                if remaining <= 0:
                    break

        elapsed_ms = (perf_counter() - start) * 1000
        throughput_mbps: float | None = None
        if elapsed_ms > 0 and bytes_read > 0:
            throughput_mbps = (bytes_read * 8 / 1_000_000) / (elapsed_ms / 1000)

        return DownloadSample(
            ok=True,
            status=status,
            elapsed_ms=round(elapsed_ms, 3),
            bytes_read=bytes_read,
            throughput_mbps=round(throughput_mbps, 3) if throughput_mbps is not None else None,
            error=None,
        )
    except (urllib3_exceptions.HTTPError, OSError, ssl.SSLError) as exc:
        return DownloadSample(
            ok=False,
            status=None,
            elapsed_ms=None,
            bytes_read=None,
            throughput_mbps=None,
            error=str(exc),
        )
    finally:
        if response is not None:
            response.release_conn()
            response.close()


def _merge_probe_and_download(*, probe_sample: ProbeSample, download_sample: DownloadSample | None) -> ProbeSample:
    """合并连通探测结果与下载探测结果."""
    if download_sample is None:
        return probe_sample

    merged_ok = probe_sample.ok and download_sample.ok
    merged_error_parts = [message for message in (probe_sample.error, download_sample.error) if message]
    merged_error = " | ".join(merged_error_parts) if merged_error_parts else None

    return ProbeSample(
        ok=merged_ok,
        tcp_ms=probe_sample.tcp_ms,
        tls_ms=probe_sample.tls_ms,
        ttfb_ms=probe_sample.ttfb_ms,
        status_line=probe_sample.status_line,
        error=merged_error,
        download_ok=download_sample.ok,
        download_status=download_sample.status,
        download_ms=download_sample.elapsed_ms,
        download_bytes=download_sample.bytes_read,
        download_mbps=download_sample.throughput_mbps,
        download_error=download_sample.error,
    )


def _median_or_none(values: list[float]) -> float | None:
    """返回浮点列表的中位数，空列表返回 None."""
    if not values:
        return None
    return round(float(median(values)), 3)


def _probe_domain_ip(
    *,
    domain: str,
    ip: str,
    timeout: float,
    iterations: int,
    probe_path: str,
    download_path: str | None,
    download_max_bytes: int,
) -> DomainIpProbeResult:
    """聚合单个域名-IP 的多次探测结果."""
    samples: list[ProbeSample] = []
    download_pool: urllib3.HTTPSConnectionPool | None = None

    if download_path is not None:
        download_pool = _build_direct_ip_pool(domain=domain, ip=ip, timeout=timeout)

    try:
        for _ in range(iterations):
            probe_sample = _probe_https_once(
                domain=domain,
                ip=ip,
                request_path=probe_path,
                timeout=timeout,
            )
            download_sample: DownloadSample | None = None
            if download_pool is not None and download_path is not None:
                download_sample = _probe_download_once(
                    pool=download_pool,
                    domain=domain,
                    request_path=download_path,
                    download_max_bytes=download_max_bytes,
                )
            samples.append(
                _merge_probe_and_download(
                    probe_sample=probe_sample,
                    download_sample=download_sample,
                )
            )
    finally:
        if download_pool is not None:
            download_pool.close()

    success_samples = [sample for sample in samples if sample.ok]
    tcp_values = [sample.tcp_ms for sample in success_samples if sample.tcp_ms is not None]
    tls_values = [sample.tls_ms for sample in success_samples if sample.tls_ms is not None]
    ttfb_values = [sample.ttfb_ms for sample in success_samples if sample.ttfb_ms is not None]

    download_attempt_samples = [sample for sample in samples if sample.download_ok is not None]
    download_success_samples = [sample for sample in download_attempt_samples if sample.download_ok]
    download_ms_values = [sample.download_ms for sample in download_success_samples if sample.download_ms is not None]
    download_bytes_values = [
        float(sample.download_bytes) for sample in download_success_samples if sample.download_bytes is not None
    ]
    download_mbps_values = [sample.download_mbps for sample in download_success_samples if sample.download_mbps is not None]

    status_lines = tuple(sorted({sample.status_line for sample in samples if sample.status_line}, key=str))
    sample_download_statuses = tuple(sorted({sample.download_status for sample in samples if sample.download_status is not None}))
    error_messages = tuple(sorted({sample.error for sample in samples if sample.error}, key=str))

    success_count = len(success_samples)
    success_rate = round(success_count / iterations, 4) if iterations > 0 else 0.0
    download_attempts = len(download_attempt_samples)
    download_success_count = len(download_success_samples)
    download_success_rate = round(download_success_count / download_attempts, 4) if download_attempts > 0 else None

    return DomainIpProbeResult(
        domain=domain,
        ip=ip,
        attempts=iterations,
        success_count=success_count,
        success_rate=success_rate,
        download_attempts=download_attempts,
        download_success_count=download_success_count,
        download_success_rate=download_success_rate,
        median_tcp_ms=_median_or_none(tcp_values),
        median_tls_ms=_median_or_none(tls_values),
        median_ttfb_ms=_median_or_none(ttfb_values),
        median_download_ms=_median_or_none(download_ms_values),
        median_download_bytes=_median_or_none(download_bytes_values),
        median_download_mbps=_median_or_none(download_mbps_values),
        sample_status_lines=status_lines,
        sample_download_statuses=sample_download_statuses,
        sample_errors=error_messages,
    )


def _build_result_rank_key(item: DomainIpProbeResult) -> tuple[float, float, float, float, float, float, str]:
    """构造统一排序键，用于域内优选与跨域总决赛."""
    return (
        -item.success_rate,
        -(item.download_success_rate if item.download_success_rate is not None else -1.0),
        -(item.median_download_mbps if item.median_download_mbps is not None else -1.0),
        item.median_ttfb_ms if item.median_ttfb_ms is not None else float("inf"),
        item.median_tls_ms if item.median_tls_ms is not None else float("inf"),
        item.median_tcp_ms if item.median_tcp_ms is not None else float("inf"),
        item.ip,
    )


def _sort_probe_results(results: list[DomainIpProbeResult]) -> list[DomainIpProbeResult]:
    """按成功率、下载表现与时延对探测结果排序."""
    return sorted(results, key=_build_result_rank_key)


def _pick_domain_best(
    *,
    domains: tuple[str, ...],
    ranked_results: Mapping[str, list[DomainIpProbeResult]],
) -> dict[str, DomainIpProbeResult]:
    """提取每个域名的最优 IP 结果."""
    best: dict[str, DomainIpProbeResult] = {}
    for domain in domains:
        domain_results = ranked_results.get(domain, [])
        if domain_results:
            best[domain] = domain_results[0]
    return best


def _pick_global_best(domain_best: Mapping[str, DomainIpProbeResult]) -> DomainIpProbeResult | None:
    """在各域名最优结果中选择全局更优 CDN."""
    if not domain_best:
        return None
    return sorted(domain_best.values(), key=_build_result_rank_key)[0]


def _build_hosts_block(domain_ip_map: Mapping[str, str]) -> list[str]:
    """渲染受管 hosts 配置区块."""
    lines = [_HOSTS_BLOCK_BEGIN]
    for domain, ip in domain_ip_map.items():
        lines.append(f"{ip} {domain} {_HOSTS_MANAGED_COMMENT}")
    lines.append(_HOSTS_BLOCK_END)
    return lines


def _replace_hosts_block(existing_lines: list[str], new_block: list[str]) -> list[str]:
    """替换或追加受管 hosts 区块."""
    begin_index: int | None = None
    end_index: int | None = None

    for idx, line in enumerate(existing_lines):
        stripped = line.strip()
        if stripped == _HOSTS_BLOCK_BEGIN:
            begin_index = idx
        if stripped == _HOSTS_BLOCK_END and begin_index is not None and idx >= begin_index:
            end_index = idx
            break

    if begin_index is not None and end_index is not None and begin_index <= end_index:
        return existing_lines[:begin_index] + new_block + existing_lines[end_index + 1 :]

    filtered = [
        line
        for line in existing_lines
        if line.strip() not in {_HOSTS_BLOCK_BEGIN, _HOSTS_BLOCK_END} and _HOSTS_MANAGED_COMMENT not in line
    ]
    if filtered and filtered[-1].strip():
        filtered.append("")
    return filtered + new_block


def _write_hosts_mapping(
    *,
    hosts_path: Path,
    domain_ip_map: Mapping[str, str],
    create_backup: bool,
) -> Path | None:
    """把优选 IP 映射写入 hosts 受管区块."""
    if not domain_ip_map:
        raise ValueError("没有可写入 hosts 的域名-IP 映射。")

    original_content = hosts_path.read_text(encoding="utf-8", errors="replace")
    backup_path: Path | None = None
    if create_backup:
        backup_path = hosts_path.with_name(f"{hosts_path.name}{DEFAULT_HOSTS_BACKUP_SUFFIX}")
        backup_path.write_text(original_content, encoding="utf-8")

    existing_lines = original_content.splitlines()
    updated_lines = _replace_hosts_block(existing_lines, _build_hosts_block(domain_ip_map))
    updated_content = "\n".join(updated_lines).rstrip("\n") + "\n"
    hosts_path.write_text(updated_content, encoding="utf-8")
    return backup_path


def _build_output_payload(
    *,
    domains: tuple[str, ...],
    resolve_records: list[IpResolveRecord],
    probe_results: list[DomainIpProbeResult],
    iterations: int,
    timeout: float,
    concurrency: int,
    probe_path: str,
    download_path: str | None,
    download_max_bytes: int,
    domain_best: Mapping[str, DomainIpProbeResult],
    global_best: DomainIpProbeResult | None,
) -> dict[str, object]:
    """构建最终 JSON 输出结构."""
    grouped_results: dict[str, list[dict[str, object]]] = defaultdict(list)
    sorted_results = _sort_probe_results(probe_results)
    for item in sorted_results:
        grouped_results[item.domain].append(asdict(item))

    domain_best_payload = {domain: asdict(result) for domain, result in domain_best.items()}
    program_recommendation: dict[str, str] | None = None
    if global_best is not None:
        program_recommendation = {
            "domain": global_best.domain,
            "ip": global_best.ip,
            "base_url": f"https://{global_best.domain}/",
        }

    return {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),  # noqa: UP017
        "domains": domains,
        "probe_config": {
            "iterations": iterations,
            "timeout_seconds": timeout,
            "concurrency": concurrency,
            "probe_path": probe_path,
            "download_path": download_path,
            "download_max_bytes": download_max_bytes,
        },
        "domain_best": domain_best_payload,
        "faster_cdn": asdict(global_best) if global_best is not None else None,
        "program_recommendation": program_recommendation,
        "dns_records": [asdict(item) for item in resolve_records],
        "ranked_results": grouped_results,
    }


def _print_summary(
    *,
    domains: tuple[str, ...],
    resolved_ips: dict[str, tuple[str, ...]],
    ranked_results: Mapping[str, list[DomainIpProbeResult]],
    domain_best: Mapping[str, DomainIpProbeResult],
    global_best: DomainIpProbeResult | None,
    top: int,
) -> None:
    """打印终端摘要结果."""
    print("=== CDN IP 优选摘要 ===")
    for domain in domains:
        ips = resolved_ips.get(domain, tuple())
        print(f"- {domain}: 候选 IP {len(ips)} 个")
        domain_results = ranked_results.get(domain, [])
        if not domain_results:
            print("  无可用探测结果")
            continue

        best = domain_best.get(domain)
        if best is not None:
            print(
                "  域名最优: "
                f"{best.ip} | success={best.success_count}/{best.attempts} "
                f"| dl={best.download_success_count}/{best.download_attempts} "
                f"| dl_mbps={best.median_download_mbps}"
            )

        for index, item in enumerate(domain_results[:top], start=1):
            print(
                "  "
                f"{index:02d}. {item.ip} | success={item.success_count}/{item.attempts} "
                f"| dl={item.download_success_count}/{item.download_attempts} "
                f"| dl_mbps={item.median_download_mbps} | ttfb={item.median_ttfb_ms}ms"
            )

    print("\n=== 跨 CDN 总决赛 ===")
    if global_best is None:
        print("未得到可比较的 CDN 结果。")
        return

    for domain in domains:
        candidate = domain_best.get(domain)
        if candidate is None:
            continue
        print(f"- {domain}: best_ip={candidate.ip}, dl_mbps={candidate.median_download_mbps}, ttfb={candidate.median_ttfb_ms}ms")

    print(
        "更快 CDN: "
        f"{global_best.domain} | ip={global_best.ip} "
        f"| dl_mbps={global_best.median_download_mbps} "
        f"| ttfb={global_best.median_ttfb_ms}ms"
    )


def main() -> None:
    """脚本入口."""
    parser = _build_argument_parser()
    args = parser.parse_args()

    if args.iterations <= 0:
        raise ValueError("--iterations 必须大于 0。")
    if args.timeout <= 0:
        raise ValueError("--timeout 必须大于 0。")
    if args.concurrency <= 0:
        raise ValueError("--concurrency 必须大于 0。")
    if args.top <= 0:
        raise ValueError("--top 必须大于 0。")
    if args.download_max_bytes < 0:
        raise ValueError("--download-max-bytes 不能小于 0。")

    domains = _normalize_domains(args.domain)
    download_path = _normalize_optional_request_path(args.download_path)
    probe_path = _normalize_request_path(args.probe_path) if args.probe_path.strip() else (download_path or "/")

    resolve_records, resolved_ips = _collect_candidate_ips(
        domains=domains,
        timeout=args.timeout,
        skip_system_resolver=args.skip_system_resolver,
    )

    probe_jobs: list[tuple[str, str]] = []
    for domain in domains:
        for ip in resolved_ips.get(domain, tuple()):
            probe_jobs.append((domain, ip))

    probe_results: list[DomainIpProbeResult] = []
    with ThreadPoolExecutor(
        max_workers=args.concurrency,
        thread_name_prefix="cdn-ip-probe",
    ) as executor:
        future_map = {
            executor.submit(
                _probe_domain_ip,
                domain=domain,
                ip=ip,
                timeout=args.timeout,
                iterations=args.iterations,
                probe_path=probe_path,
                download_path=download_path,
                download_max_bytes=args.download_max_bytes,
            ): (domain, ip)
            for domain, ip in probe_jobs
        }
        for future in as_completed(future_map):
            probe_results.append(future.result())

    sorted_results = _sort_probe_results(probe_results)
    grouped_sorted_results: dict[str, list[DomainIpProbeResult]] = defaultdict(list)
    for item in sorted_results:
        grouped_sorted_results[item.domain].append(item)

    domain_best = _pick_domain_best(
        domains=domains,
        ranked_results=grouped_sorted_results,
    )
    global_best = _pick_global_best(domain_best)

    _print_summary(
        domains=domains,
        resolved_ips=resolved_ips,
        ranked_results=grouped_sorted_results,
        domain_best=domain_best,
        global_best=global_best,
        top=args.top,
    )

    if args.apply_hosts:
        domain_ip_map = {domain: result.ip for domain, result in domain_best.items()}
        if len(domain_ip_map) < 1:
            raise ValueError("没有可写入 hosts 的优选结果。")
        try:
            backup_path = _write_hosts_mapping(
                hosts_path=args.hosts_file,
                domain_ip_map=domain_ip_map,
                create_backup=args.hosts_backup,
            )
        except PermissionError as exc:
            raise PermissionError(f"写入 hosts 失败：{args.hosts_file}，请使用管理员权限执行。") from exc

        print(f"\nhosts 已写入: {args.hosts_file}")
        if backup_path is not None:
            print(f"hosts 备份: {backup_path}")

    payload = _build_output_payload(
        domains=domains,
        resolve_records=resolve_records,
        probe_results=probe_results,
        iterations=args.iterations,
        timeout=args.timeout,
        concurrency=args.concurrency,
        probe_path=probe_path,
        download_path=download_path,
        download_max_bytes=args.download_max_bytes,
        domain_best=domain_best,
        global_best=global_best,
    )
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n详细报告已输出: {args.output_json}")


if __name__ == "__main__":
    main()
