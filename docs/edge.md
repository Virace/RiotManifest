# 边缘 IP 优选（可选）

`riotmanifest.edge` 是可选的稳定性兜底层：多解析源发现 riotcdn 候选 IP，
真实探测打分后选出赢家池，把下载连接轮转分摊到 2-3 个优质边缘节点，并周期刷新。

## 定位：稳定兜底，不是提速

riotcdn 域名背后由多家 CDN 加权轮换（CNAME TTL 只有秒-分钟级），各节点的
下载吞吐相当均匀。本层的价值是**避开劣质节点与 DNS 异常时段**，而不是提高
峰值速度；仅在请求延迟主导的稀疏下载场景（如只取单个 WAD）有次要的提速效果
（更低 RTT 边缘可压低每请求等待）。

任何环节失败（依赖缺失除外）都会优雅回退系统 DNS——启用后行为不会劣于未启用。

## 安装

```bash
pip install riotmanifest[edge]
```

未安装 `dnspython` 时导入 `riotmanifest.edge` 不报错，进入 `EdgeSelector`
上下文时报 `RuntimeError` 并提示安装命令。

## 用法

```python
import asyncio
from riotmanifest import PatcherManifest
from riotmanifest.edge import EdgeSelector


async def main() -> None:
    manifest = PatcherManifest(manifest_url, path="./out")

    async with EdgeSelector.for_manifest(manifest) as selector:
        manifest.resolver = selector.resolver
        files = list(manifest.filter_files(pattern="wad.client"))
        await manifest.download_files_concurrently(files)


asyncio.run(main())
```

也可以在构造 `PatcherManifest` 时直接传 `resolver=selector.resolver`。

`for_manifest` 会自动：

- 从 `bundle_urls`（或 `bundle_url`）提取受管域名；
- 选一个"总大小 ≥1MB 的最小 bundle"作为探测目标（全部小于 1MB 时取最大者）。

独立构造（不依赖 manifest 实例）：

```python
EdgeSelector(
    hostnames=["lol.dyn.riotcdn.net", "lol.secure.dyn.riotcdn.net"],
    probe_url="https://lol.dyn.riotcdn.net/channels/public/bundles/<BUNDLE_ID>.bundle",
)
```

## 构造参数

均为可改写的默认值：

- `winners=3`：赢家池容量，按探测 TTFB 升序取前 N
- `refresh_interval=60.0`：赢家池刷新间隔秒数（对齐官方 patcher 每分钟重查 DNS 的做法）
- `probe_range_bytes=1024 * 1024`：单次探测的 Range 下载字节数
- `dns_servers=("223.5.5.5", "119.29.29.29", "1.1.1.1")`：UDP 直查服务器
- `doh_url="https://223.5.5.5/dns-query"`：DoH 端点（RFC 8484）
- `probe_timeout=10.0`：单候选探测超时秒数

## 工作机制

1. **候选发现**（每域名三源并集，单源失败跳过）：系统 DNS、UDP 直查、
   DoH 带 ECS（出口网段提示可挖出普通解析拿不到的区域边缘；出口 IP 探测
   失败时自动降级为不带 ECS）。
2. **探测打分**：对每个候选 IP 固定连接（SNI 与证书校验仍按原域名）做真实
   Range 下载，按 TTFB 排序；探测不通过的 IP 不入池。
3. **连接分摊**：resolver 对受管域名每次解析轮转赢家起点，新建连接均匀落在
   各赢家 IP；非受管域名与池空场景委托系统 DNS。
4. **刷新与回退**：上下文存续期间每 `refresh_interval` 重跑发现+探测并整体
   替换赢家池；单轮失败保留旧池；退出上下文即停止刷新，无后台任务泄漏。

## 与下载器的配合

`PatcherManifest(resolver=...)` 注入后，下载批次的连接解析交由该 resolver，
且自动禁用 aiohttp 内建 DNS 缓存（保证轮转生效）。重试跨域名切换
（`bundle_urls`）与本层叠加使用：域名层换供应商、IP 层换边缘，互不冲突。
