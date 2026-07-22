"""EdgeSelector 单元测试：网络与 DNS 交互全部 mock，仅 DNS 报文构造使用真实 dnspython."""

from __future__ import annotations

import asyncio
import base64
import socket
import sys
import types

import pytest

from riotmanifest import edge
from riotmanifest.edge import EdgeSelector
from riotmanifest.manifest import PatcherBundle
from riotmanifest.utils.http_client import HttpResponse

MANAGED_HOST = "lol.dyn.riotcdn.net"
PROBE_URL = f"https://{MANAGED_HOST}/channels/public/bundles/00000000000000AB.bundle"


def _make_selector(**kwargs) -> EdgeSelector:
    return EdgeSelector([MANAGED_HOST], PROBE_URL, **kwargs)


def _make_bundle(bundle_id: int, chunk_sizes: list[int]) -> PatcherBundle:
    bundle = PatcherBundle(bundle_id)
    for index, size in enumerate(chunk_sizes):
        bundle.add_chunk(chunk_id=bundle_id * 1000 + index, size=size, target_size=size)
    return bundle


class _StubDefaultResolver:
    """替身 DefaultResolver：记录调用并返回固定结果，避免真实 DNS."""

    def __init__(self):
        self.calls: list[tuple[str, int, int]] = []

    async def resolve(self, host, port=0, family=socket.AF_INET):
        self.calls.append((host, port, family))
        return [{"hostname": host, "host": "9.9.9.9", "port": port, "family": socket.AF_INET, "proto": 0, "flags": 0}]

    async def close(self):
        pass


def test_resolver_returns_winners_and_rotates():
    selector = _make_selector()
    selector._winners = ("1.0.0.1", "1.0.0.2", "1.0.0.3")

    async def scenario():
        first = await selector.resolver.resolve(MANAGED_HOST, port=443)
        second = await selector.resolver.resolve(MANAGED_HOST, port=443)
        third = await selector.resolver.resolve(MANAGED_HOST, port=443)
        return first, second, third

    first, second, third = asyncio.run(scenario())
    assert [item["host"] for item in first] == ["1.0.0.1", "1.0.0.2", "1.0.0.3"]
    assert [item["host"] for item in second] == ["1.0.0.2", "1.0.0.3", "1.0.0.1"]
    assert [item["host"] for item in third] == ["1.0.0.3", "1.0.0.1", "1.0.0.2"]
    entry = first[0]
    assert entry["hostname"] == MANAGED_HOST
    assert entry["port"] == 443
    assert entry["family"] == socket.AF_INET
    assert entry["proto"] == 0
    assert entry["flags"] == socket.AI_NUMERICHOST


def test_empty_pool_delegates_to_default_resolver(monkeypatch):
    created: list[_StubDefaultResolver] = []

    def factory():
        stub = _StubDefaultResolver()
        created.append(stub)
        return stub

    monkeypatch.setattr(edge, "DefaultResolver", factory)
    selector = _make_selector()

    result = asyncio.run(selector.resolver.resolve(MANAGED_HOST, port=443))

    assert result[0]["host"] == "9.9.9.9"
    assert len(created) == 1
    assert created[0].calls == [(MANAGED_HOST, 443, socket.AF_INET)]


def test_unmanaged_host_delegates_to_default_resolver(monkeypatch):
    created: list[_StubDefaultResolver] = []

    def factory():
        stub = _StubDefaultResolver()
        created.append(stub)
        return stub

    monkeypatch.setattr(edge, "DefaultResolver", factory)
    selector = _make_selector()
    selector._winners = ("1.0.0.1",)

    result = asyncio.run(selector.resolver.resolve("example.com", port=443))

    assert result[0]["host"] == "9.9.9.9"
    assert created[0].calls == [("example.com", 443, socket.AF_INET)]
    # 受管域名不受影响，仍命中赢家池
    hit = asyncio.run(selector.resolver.resolve(MANAGED_HOST, port=443))
    assert [item["host"] for item in hit] == ["1.0.0.1"]


def test_failed_probe_excluded_from_winners(monkeypatch):
    selector = _make_selector(winners=3)
    monkeypatch.setattr(EdgeSelector, "_discover", lambda self: {"10.0.0.1", "10.0.0.2", "10.0.0.3"})

    def fake_probe(ip, probe_url, range_bytes, timeout):
        assert probe_url == PROBE_URL
        if ip == "10.0.0.2":
            return None
        return (0.02, 0.2) if ip == "10.0.0.3" else (0.05, 0.5)

    monkeypatch.setattr(edge, "_probe_ip", fake_probe)

    asyncio.run(selector._refresh())

    assert selector._winners == ("10.0.0.3", "10.0.0.1")


def test_missing_dnspython_raises_on_enter(monkeypatch):
    for name in ("dns", "dns.edns", "dns.message", "dns.query", "dns.rdatatype"):
        monkeypatch.setitem(sys.modules, name, None)

    selector = _make_selector()  # 仅构造不进入 context 不应报错

    with pytest.raises(RuntimeError, match=r"riotmanifest\[edge\]"):
        asyncio.run(selector.__aenter__())


def test_for_manifest_picks_smallest_bundle_at_least_1mb():
    manifest = types.SimpleNamespace(
        bundle_urls=[
            "https://a.example.com/bundles/",
            "https://b.example.com/bundles/",
            "https://a.example.com/bundles/",
        ],
        bundles=[
            _make_bundle(0x1, [512 * 1024]),
            _make_bundle(0x2, [1024 * 1024, 1024 * 1024]),
            _make_bundle(0x3, [4 * 1024 * 1024]),
        ],
    )

    selector = EdgeSelector.for_manifest(manifest, winners=2)

    assert selector._hostnames == ("a.example.com", "b.example.com")
    assert selector._probe_url == f"https://a.example.com/bundles/{0x2:016X}.bundle"
    assert selector._winner_count == 2


def test_for_manifest_all_small_picks_largest_with_bundle_url_fallback():
    manifest = types.SimpleNamespace(
        bundle_url="https://cdn.example.com/bundles/",
        bundles=[
            _make_bundle(0xA, [100]),
            _make_bundle(0xB, [300]),
            _make_bundle(0xC, [200]),
        ],
    )

    selector = EdgeSelector.for_manifest(manifest)

    assert selector._hostnames == ("cdn.example.com",)
    assert selector._probe_url == f"https://cdn.example.com/bundles/{0xB:016X}.bundle"


def test_lifecycle_refresh_loop_runs_and_stops(monkeypatch):
    calls = {"count": 0}

    async def fake_refresh(self):
        calls["count"] += 1

    monkeypatch.setattr(EdgeSelector, "_refresh", fake_refresh)

    async def scenario():
        selector = _make_selector(refresh_interval=0.01)
        async with selector:
            task = selector._refresh_task
            assert task is not None
            await asyncio.sleep(0.3)
        assert selector._refresh_task is None
        assert task.done()

    asyncio.run(scenario())

    # 首轮 + 至少两轮周期刷新
    assert calls["count"] >= 3


def test_doh_wire_roundtrip_with_ecs(monkeypatch):
    import dns.edns
    import dns.message
    import dns.rdatatype
    import dns.rrset

    captured = {}

    def fake_http_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        param = url.split("dns=", 1)[1]
        wire = base64.urlsafe_b64decode(param + "=" * (-len(param) % 4))
        query = dns.message.from_wire(wire)
        captured["query"] = query
        reply = dns.message.make_response(query)
        reply.answer.append(dns.rrset.from_text(query.question[0].name, 60, "IN", "A", "104.16.1.2"))
        return HttpResponse(status=200, data=reply.to_wire(), headers={})

    monkeypatch.setattr(edge, "http_get", fake_http_get)

    ips = edge._doh_ips(MANAGED_HOST, "https://223.5.5.5/dns-query", "203.0.113.77", timeout=1.0)

    assert ips == {"104.16.1.2"}
    assert captured["url"].startswith("https://223.5.5.5/dns-query?dns=")
    assert "=" not in captured["url"].split("dns=", 1)[1]  # base64url 无填充
    assert captured["headers"]["Accept"] == "application/dns-message"
    query = captured["query"]
    question = query.question[0]
    assert question.name.to_text() == f"{MANAGED_HOST}."
    assert question.rdtype == dns.rdatatype.A
    ecs_options = [option for option in query.options if isinstance(option, dns.edns.ECSOption)]
    assert len(ecs_options) == 1
    assert ecs_options[0].srclen == 24
    assert ecs_options[0].address == "203.0.113.0"


def test_doh_without_ecs_when_client_ip_missing(monkeypatch):
    import dns.edns
    import dns.message
    import dns.rrset

    captured = {}

    def fake_http_get(url, headers=None, timeout=None):
        param = url.split("dns=", 1)[1]
        wire = base64.urlsafe_b64decode(param + "=" * (-len(param) % 4))
        query = dns.message.from_wire(wire)
        captured["query"] = query
        reply = dns.message.make_response(query)
        reply.answer.append(dns.rrset.from_text(query.question[0].name, 60, "IN", "A", "104.16.1.3"))
        return HttpResponse(status=200, data=reply.to_wire(), headers={})

    monkeypatch.setattr(edge, "http_get", fake_http_get)

    ips = edge._doh_ips(MANAGED_HOST, "https://223.5.5.5/dns-query", None, timeout=1.0)

    assert ips == {"104.16.1.3"}
    ecs_options = [option for option in captured["query"].options if isinstance(option, dns.edns.ECSOption)]
    assert ecs_options == []
