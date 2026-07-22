"""riotcdn 边缘 IP 优选层.

riotcdn 域名背后由多家 CDN 加权轮换（CNAME TTL 极短），系统 DNS 单次解析未必命中最优边缘节点。
本模块通过多解析源（系统 DNS / UDP 直查 / DoH+ECS）发现候选 IP，用真实 Range 下载探测打分后
选出赢家池，并以 aiohttp 自定义 resolver 将受管域名的连接轮转分摊到赢家 IP，周期性刷新。

定位为稳定性兜底层：dnspython 为可选运行时依赖（`pip install riotmanifest[edge]`），
任何发现 / 探测环节失败都会优雅回退系统 DNS，行为不会劣于未启用状态。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import socket
import ssl
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
from http.client import HTTPSConnection
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import DefaultResolver
from loguru import logger
from urllib3.util import Timeout

from riotmanifest.utils.http_client import http_get

if TYPE_CHECKING:
    from riotmanifest.manifest import PatcherBundle, PatcherManifest

_DNS_TIMEOUT = 3.0
_MIN_PROBE_BUNDLE_BYTES = 1024 * 1024


def _import_dns() -> Any:
    """导入 dnspython 顶层模块及所需子模块.

    dnspython 是可选依赖，业务代码必须经由本函数延迟导入，保证未安装时模块本身可正常加载。

    Returns:
        dnspython 顶层模块 `dns`（已加载 edns / message / query / rdatatype 子模块）.

    Raises:
        RuntimeError: dnspython 未安装时抛出，提示通过 `pip install riotmanifest[edge]` 安装.
    """
    try:
        import dns.edns
        import dns.message
        import dns.query
        import dns.rdatatype
    except ImportError as exc:
        raise RuntimeError("边缘 IP 优选需要 dnspython，请执行 pip install riotmanifest[edge] 安装") from exc
    return dns


def _answer_ips(reply: Any) -> set[str]:
    """提取 DNS 应答 answer 区中全部 A 记录的地址集合."""
    dns = _import_dns()
    return {item.address for rrset in reply.answer if rrset.rdtype == dns.rdatatype.A for item in rrset}


def _system_ips(host: str) -> set[str]:
    """通过系统解析器获取域名的 IPv4 地址集合（同步阻塞，需放线程池调用）."""
    infos = socket.getaddrinfo(host, 443, family=socket.AF_INET, type=socket.SOCK_STREAM)
    return {info[4][0] for info in infos}


def _udp_ips(host: str, servers: Sequence[str], timeout: float) -> set[str]:
    """通过 UDP 直查多个 DNS 服务器获取 A 记录，单服务器失败静默跳过.

    Args:
        host: 待解析域名.
        servers: DNS 服务器地址列表.
        timeout: 单次查询超时秒数.

    Returns:
        各服务器应答的 IPv4 地址并集.
    """
    dns = _import_dns()
    ips: set[str] = set()
    for server in servers:
        try:
            query = dns.message.make_query(host, "A")
            ips |= _answer_ips(dns.query.udp(query, server, timeout=timeout))
        except Exception as exc:
            logger.debug("UDP DNS 查询失败: host={}, server={}, error={}", host, server, exc)
    return ips


def _public_ip(timeout: float) -> str | None:
    """通过 whoami.cloudflare CH TXT 查询（@1.1.1.1）探测出口公网 IPv4.

    用于为 DoH 查询构造 ECS（EDNS Client Subnet）提示；失败时返回 None，
    调用方应降级为不带 ECS 的查询。

    Args:
        timeout: 查询超时秒数.

    Returns:
        出口公网 IPv4 字符串；探测失败或应答非 IPv4 时返回 None.
    """
    dns = _import_dns()
    try:
        query = dns.message.make_query("whoami.cloudflare", "TXT", rdclass="CH")
        reply = dns.query.udp(query, "1.1.1.1", timeout=timeout)
        for rrset in reply.answer:
            for item in rrset:
                text = b"".join(item.strings).decode("ascii", "ignore").strip()
                try:
                    if isinstance(ipaddress.ip_address(text), ipaddress.IPv4Address):
                        return text
                except ValueError:
                    continue
    except Exception as exc:
        logger.warning("出口 IP 探测失败，DoH 降级为不带 ECS: {}", exc)
        return None
    logger.warning("出口 IP 探测未返回 IPv4，DoH 降级为不带 ECS")
    return None


def _doh_ips(host: str, doh_url: str, client_ip: str | None, timeout: float) -> set[str]:
    """通过 DoH（RFC 8484 GET）查询 A 记录，可附带 ECS 提示出口网段.

    手工构造 wire 报文并以 base64url（无填充）放入 `?dns=` 查询参数，
    经仓库内同步 HTTP 客户端发送，避免为 DoH 引入额外 HTTP 依赖。

    Args:
        host: 待解析域名.
        doh_url: DoH 服务端点.
        client_ip: 出口公网 IP；提供时附带 srclen=24 的 ECS 选项，为 None 时不带 ECS.
        timeout: 请求超时秒数.

    Returns:
        应答中的 IPv4 地址集合.
    """
    dns = _import_dns()
    if client_ip:
        ecs = dns.edns.ECSOption(address=client_ip, srclen=24)
        query = dns.message.make_query(host, "A", use_edns=0, options=[ecs])
    else:
        query = dns.message.make_query(host, "A")
    wire = base64.urlsafe_b64encode(query.to_wire()).rstrip(b"=").decode("ascii")
    sep = "&" if "?" in doh_url else "?"
    response = http_get(
        f"{doh_url}{sep}dns={wire}",
        headers={"Accept": "application/dns-message"},
        timeout=Timeout(connect=timeout, read=timeout),
    )
    return _answer_ips(dns.message.from_wire(response.data))


class _PinnedHTTPS(HTTPSConnection):
    """连接固定 IP 但按指定域名完成 SNI 与证书校验的 HTTPS 连接."""

    def __init__(self, ip: str, server_hostname: str, *, port: int = 443, timeout: float = 10.0):
        """初始化固定 IP 连接.

        Args:
            ip: 实际建立 TCP 连接的 IPv4 地址.
            server_hostname: TLS 握手使用的域名（SNI 与证书校验均按此域名）.
            port: 目标端口.
            timeout: 连接与读取超时秒数.
        """
        super().__init__(ip, port, timeout=timeout, context=ssl.create_default_context())
        self._server_hostname = server_hostname

    def connect(self) -> None:
        """建立到固定 IP 的 TCP 连接并按 server_hostname 完成 TLS 握手."""
        sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self._server_hostname)


def _probe_ip(ip: str, probe_url: str, range_bytes: int, timeout: float) -> tuple[float, float] | None:
    """对单个候选 IP 做真实 Range 下载探测（同步阻塞，需放线程池调用）.

    固定连接到 ip，TLS 握手与 Host 头均按 probe_url 的域名，
    保证测得的是该 IP 服务目标域名的真实表现。

    Args:
        ip: 待探测的 IPv4 地址.
        probe_url: 探测目标 URL.
        range_bytes: Range 请求的下载字节数.
        timeout: 连接与读取超时秒数.

    Returns:
        (TTFB 秒, 全量读取耗时秒)；状态非 200/206、超时、SSL 错误等任何失败返回 None.
    """
    parsed = urlparse(probe_url)
    host = parsed.hostname
    if not host:
        return None
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    conn = _PinnedHTTPS(ip, host, port=parsed.port or 443, timeout=timeout)
    try:
        started = time.monotonic()
        conn.request("GET", path, headers={"Range": f"bytes=0-{range_bytes - 1}", "Host": host})
        response = conn.getresponse()
        ttfb = time.monotonic() - started
        if response.status not in (200, 206):
            logger.debug("探测状态异常: ip={}, status={}", ip, response.status)
            return None
        response.read()
        return ttfb, time.monotonic() - started
    except Exception as exc:
        logger.debug("探测失败: ip={}, error={}", ip, exc)
        return None
    finally:
        conn.close()


def _bundle_size(bundle: PatcherBundle) -> int:
    """按末尾 chunk 的 offset+size 计算 bundle 压缩体总字节数；无 chunk 时为 0."""
    if not bundle.chunks:
        return 0
    last = bundle.chunks[-1]
    return last.offset + last.size


class _EdgeResolver(AbstractResolver):
    """aiohttp 解析器：受管域名命中赢家池时返回赢家 IP，否则委托系统 DNS."""

    def __init__(self, selector: EdgeSelector):
        """绑定提供赢家池与轮转策略的选择器实例."""
        self._selector = selector
        self._default: AbstractResolver | None = None

    async def resolve(self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET) -> list[ResolveResult]:
        """解析 host：优先返回赢家 IP 列表，池空或域名不受管时回退系统 DNS.

        Args:
            host: 待解析域名.
            port: 目标端口，透传到解析结果.
            family: 期望地址族；赢家池仅支持 IPv4（AF_UNSPEC / AF_INET），其余委托系统 DNS.

        Returns:
            aiohttp 解析结果列表.
        """
        winners = self._selector._winners_for(host, family)
        if winners:
            return [
                ResolveResult(hostname=host, host=ip, port=port, family=socket.AF_INET, proto=0, flags=socket.AI_NUMERICHOST)
                for ip in winners
            ]
        if self._default is None:
            self._default = DefaultResolver()
        return await self._default.resolve(host, port, family)

    async def close(self) -> None:
        """释放内部回退解析器资源；幂等，可重复调用."""
        default, self._default = self._default, None
        if default is not None:
            await default.close()


class EdgeSelector:
    """riotcdn 边缘 IP 优选器.

    生命周期为异步上下文管理器：进入时执行首轮候选发现与探测并启动周期刷新任务，
    退出时取消并等待刷新任务结束。`resolver` 属性返回可直接交给
    `aiohttp.TCPConnector(resolver=...)` 的解析器，对受管域名按赢家池轮转返回 IP，
    赢家池为空或域名不受管时回退系统 DNS。
    """

    def __init__(
        self,
        hostnames: Sequence[str],
        probe_url: str,
        *,
        winners: int = 3,
        refresh_interval: float = 60.0,
        probe_range_bytes: int = 1024 * 1024,
        dns_servers: Sequence[str] = ("223.5.5.5", "119.29.29.29", "1.1.1.1"),
        doh_url: str = "https://223.5.5.5/dns-query",
        probe_timeout: float = 10.0,
    ):
        """初始化选择器；不触发任何网络请求，也不导入 dnspython.

        Args:
            hostnames: 受管域名集合，resolver 仅对这些域名应用赢家池.
            probe_url: 探测目标，一个真实 bundle 的完整 URL.
            winners: 赢家池容量，按 TTFB 升序取前 N 个候选.
            refresh_interval: 赢家池刷新间隔秒数.
            probe_range_bytes: 单次探测下载的 Range 字节数.
            dns_servers: UDP 直查使用的 DNS 服务器地址列表.
            doh_url: DoH 服务端点（RFC 8484 GET）.
            probe_timeout: 单个候选探测的超时秒数.
        """
        self._hostnames = tuple(dict.fromkeys(h.rstrip(".").lower() for h in hostnames))
        self._probe_url = probe_url
        self._winner_count = max(1, winners)
        self._refresh_interval = refresh_interval
        self._probe_range_bytes = probe_range_bytes
        self._dns_servers = tuple(dns_servers)
        self._doh_url = doh_url
        self._probe_timeout = probe_timeout
        self._winners: tuple[str, ...] = ()
        self._rotation = 0
        self._refresh_task: asyncio.Task[None] | None = None
        self._resolver = _EdgeResolver(self)

    async def __aenter__(self) -> EdgeSelector:
        """校验 dnspython 可用后执行首轮发现与探测，并启动周期刷新任务.

        首轮发现 / 探测失败不抛出，赢家池保持为空即回退系统 DNS；
        仅在 dnspython 缺失这类配置性错误时抛出 RuntimeError。
        """
        _import_dns()
        try:
            await self._refresh()
        except Exception as exc:
            logger.warning("EdgeSelector 首轮探测失败，暂以系统 DNS 兜底: {}", exc)
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        return self

    async def __aexit__(self, *exc: object) -> None:
        """取消并等待刷新任务结束，释放回退解析器资源，保证无任务泄漏."""
        task, self._refresh_task = self._refresh_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._resolver.close()

    @property
    def resolver(self) -> AbstractResolver:
        """返回绑定本选择器的 aiohttp 解析器；实例稳定，可长期持有."""
        return self._resolver

    @classmethod
    def for_manifest(cls, manifest: PatcherManifest, **kwargs: Any) -> EdgeSelector:
        """从 manifest 构建选择器：提取 bundle 域名并挑选合适的探测 bundle.

        探测目标取“总大小 ≥1MB 的最小 bundle”，兼顾探测代表性与流量开销；
        全部不足 1MB 时取最大者。

        Args:
            manifest: 已解析的 manifest 实例；优先读取 `bundle_urls` 列表，
                不存在时退回单一 `bundle_url`.
            **kwargs: 透传给构造函数的其余关键字参数.

        Returns:
            配置好受管域名与探测 URL 的选择器实例.

        Raises:
            ValueError: manifest 不含任何带 chunk 的 bundle 时抛出.
        """
        urls: list[str] = list(getattr(manifest, "bundle_urls", None) or [manifest.bundle_url])
        hostnames: list[str] = []
        for url in urls:
            host = urlparse(url).hostname
            if host and host not in hostnames:
                hostnames.append(host)
        bundles = [bundle for bundle in manifest.bundles if bundle.chunks]
        if not bundles:
            raise ValueError("manifest 不含任何带 chunk 的 bundle，无法选择探测目标")
        eligible = [bundle for bundle in bundles if _bundle_size(bundle) >= _MIN_PROBE_BUNDLE_BYTES]
        target = min(eligible, key=_bundle_size) if eligible else max(bundles, key=_bundle_size)
        probe_url = urljoin(urls[0], f"{target.bundle_id:016X}.bundle")
        return cls(hostnames, probe_url, **kwargs)

    def _winners_for(self, host: str, family: int) -> list[str]:
        """返回 host 对应的轮转赢家 IP 列表；不受管、池空或地址族不兼容时返回空列表.

        每次命中都会推进轮转起点，使新建连接均匀分摊到各赢家 IP。
        """
        winners = self._winners
        if not winners or family not in (socket.AF_UNSPEC, socket.AF_INET):
            return []
        if host.rstrip(".").lower() not in self._hostnames:
            return []
        start = self._rotation % len(winners)
        self._rotation += 1
        return list(winners[start:] + winners[:start])

    def _discover(self) -> set[str]:
        """聚合多解析源的候选 IPv4 集合（同步阻塞，需放线程池调用）.

        各解析源相互独立，单源失败仅记录告警并跳过，不影响其余来源。
        """
        client_ip = _public_ip(_DNS_TIMEOUT)
        candidates: set[str] = set()
        for host in self._hostnames:
            sources: tuple[tuple[str, Callable[[], set[str]]], ...] = (
                ("system", partial(_system_ips, host)),
                ("udp", partial(_udp_ips, host, self._dns_servers, _DNS_TIMEOUT)),
                ("doh", partial(_doh_ips, host, self._doh_url, client_ip, _DNS_TIMEOUT)),
            )
            for name, fetch in sources:
                try:
                    candidates |= fetch()
                except Exception as exc:
                    logger.warning("候选发现失败: source={}, host={}, error={}", name, host, exc)
        return candidates

    def _probe_all(self, candidates: Sequence[str]) -> list[tuple[str, float, float]]:
        """并行探测候选 IP 并按 TTFB 升序排序（同步阻塞，需放线程池调用）.

        Returns:
            (ip, TTFB 秒, 全量读取耗时秒) 列表，仅包含探测成功的候选.
        """
        ranked: list[tuple[str, float, float]] = []
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(candidates)))) as pool:
            futures = {
                pool.submit(_probe_ip, ip, self._probe_url, self._probe_range_bytes, self._probe_timeout): ip
                for ip in candidates
            }
            for future in as_completed(futures):
                try:
                    timing = future.result()
                except Exception as exc:
                    logger.warning("探测异常: ip={}, error={}", futures[future], exc)
                    continue
                if timing is not None:
                    ranked.append((futures[future], timing[0], timing[1]))
        ranked.sort(key=lambda item: item[1])
        return ranked

    async def _refresh(self) -> None:
        """执行一轮候选发现与探测打分，并整体原子替换赢家池.

        发现结果为空或全部探测失败时赢家池置空，resolver 随之回退系统 DNS。
        """
        started = time.monotonic()
        candidates = await asyncio.to_thread(self._discover)
        ranked: list[tuple[str, float, float]] = []
        if candidates:
            ranked = await asyncio.to_thread(self._probe_all, sorted(candidates))
        self._winners = tuple(ip for ip, _, _ in ranked[: self._winner_count])
        elapsed = time.monotonic() - started
        if self._winners:
            logger.info(
                "EdgeSelector 赢家更新: winners={}, candidates={}, elapsed={:.2f}s",
                self._winners,
                len(candidates),
                elapsed,
            )
        else:
            logger.warning(
                "EdgeSelector 无可用赢家，回退系统 DNS: candidates={}, elapsed={:.2f}s",
                len(candidates),
                elapsed,
            )

    async def _refresh_loop(self) -> None:
        """按 refresh_interval 周期刷新赢家池；单轮失败记录告警后继续下一轮."""
        while True:
            await asyncio.sleep(self._refresh_interval)
            try:
                await self._refresh()
            except Exception as exc:
                logger.warning("EdgeSelector 刷新失败，保留现有赢家池: {}", exc)
