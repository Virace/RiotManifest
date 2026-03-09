# League Manifest 工具参考

## 作用范围

本文件说明 `riotmanifest.game` 当前面向英雄联盟（LoL）的两条能力线：

- `LeagueManifestResolver`
  - 面向“当前可用区域”的官方接口聚合器
  - 负责把 LCU 与 GAME manifest 整理成一套稳定可用的公开 API
- `LeagueManifestInspector`
  - 面向“已经拿到 manifest 文件或 URL”的检查器
  - 负责识别类型、提取版本，并在双清单场景下判断能否组成一对

定义位置：

- `src/riotmanifest/game/factory.py`
- `src/riotmanifest/game/inspection.py`
- `src/riotmanifest/game/metadata.py`

## 先统一一个心智模型

### 对外只讲一个概念：`region`

对使用者来说，这个模块只要求你提供一个 `region`。

常见输入示例：

- `EUW`
- `EUW1`
- `PBE`
- `PBE1`
- `KR`
- `BR`

`LeagueManifestResolver` 会在内部自行完成这些 Riot 底层概念的映射：

- LCU `patchline`，例如 `live` / `pbe`
- clientconfig 配置 `id`
- launcher `--region=`
- GAME `version-set`，例如 `EUW1` / `PBE1`

这些概念在 Riot 官方接口里经常重名、重叠，甚至同一地区有时写成 `BR`，有时写成 `BR1`。
**本模块不要求调用方理解这套底层命名空间。**

### 推荐输入习惯

虽然 `EUW` / `EUW1`、`PBE` / `PBE1` 都能识别，但文档与示例统一推荐：

- Live 区域优先写 `EUW`、`KR`、`BR` 这类主区域名
- PBE 优先写 `PBE`

理由很简单：

- 这更符合 `LeagueManifestResolver` 的公开心智
- 不会把 Riot 的 `version-set` 细节继续暴露给上层业务
- 迁移旧代码时也更容易看出“这是用户输入区域”，而不是内部配置 ID

### 当前可用区域（截至 2026-03-09）

`LeagueManifestResolver.available_regions()` 当前会基于 Riot 的 patchlines `clientconfig` 动态产出以下区域：

- `BR`
- `EUNE`
- `EUW`
- `JP`
- `KR`
- `LA1`
- `LA2`
- `ME1`
- `NA`
- `OC1`
- `PBE`
- `RU`
- `SG2`
- `TR`
- `TW2`
- `VN2`

当前常见 alias 中，以下输入会收敛到规范化后的公开区域：

- `BR1 -> BR`
- `EUN1 -> EUNE`
- `EUW1 -> EUW`
- `JP1 -> JP`
- `NA1 -> NA`
- `TR1 -> TR`
- `PBE1 -> PBE`

说明：

- 这里列的是当前 Resolver 能实际解析的输入，不是 Riot Developer 文档里全部平台路由值的原样镜像。
- 该列表来自运行时读取 `https://clientconfig.rpg.riotgames.com/api/v1/config/public?namespace=keystone.products.league_of_legends.patchlines` 的结果，未来可能随 Riot 上游调整而变化。

### 历史或当前不可用的区域标识

以下标识在 Riot 生态里仍可能出现在旧代码、第三方库、历史 manifest 仓库或 API 路由常量中，但当前 `LeagueManifestResolver` 不会把它们视为可用输入：

- `PH2`
- `TH2`

当前判断依据：

- Riot Developer LoL 文档仍能看到 `PH2`、`TH2` 这类平台路由值。
- 第三方项目和历史清单仓库中也仍能看到 `PH2`、`TH2` 对应目录或 API host。
- 但截至 2026-03-09，Riot patchlines `clientconfig` 中的 LoL live/pbe 配置并未暴露 `PH2`、`TH2`，因此当前 Resolver 无法将它们解析为有效区域。

如果你传入 `PH2` 或 `TH2`，当前会得到 `RegionConfigNotFoundError`。

另一个容易混淆的概念是：

- `SEA`

`SEA` 不是和 `PH2`、`TH2`、`SG2`、`TW2`、`VN2` 同层的 patchline 区域输入。它更常见于 Riot API 的 regional routing 语义或第三方库的分组概念；当前 `LeagueManifestResolver` 也不会接受它作为可解析区域。

## 两个类分别做什么

### `LeagueManifestResolver`

它不是完整历史版本管理器，也不是 Riot 所有清单接口的原样透传。
当前职责收敛为：

- 解析 Riot 官方当前可用的 LoL 区域配置
- 根据给定 `region` 找到对应的 LCU manifest
- 根据同一 `region` 找到当前可见的 GAME 候选集合
- 按明确的匹配规则构造一对 LCU/GAME manifest
- 提供统一的版本对象给上层消费

### `LeagueManifestInspector`

它不依赖“当前 live / pbe 配置”，也不要求你先知道区域。
当前职责是：

- 从单个 manifest 文件或 URL 出发识别它是 `lcu`、`game` 还是 `unknown`
- 尽量从内容中提取版本信息
- 在双清单场景下判断它们能否组成一对

## 数据源边界

### Resolver 使用的上游接口

#### LCU 配置来源：`clientconfig.rpg.riotgames.com`

这里的价值在于：

- 可以拿到当前可用的 LCU manifest 地址
- 可以拿到与该配置相关的一组区域别名
- 可以拿到 GAME `version-set` 所需的 patchsieve 配置

需要注意：

- `metadata.theme_manifest` 现在只作为弱提示，不是严格版本依据
- 旧的 `secondary_patchlines.game_patch` 不再作为可靠来源

#### GAME 候选来源：`sieve.services.riotcdn.net`

这里的价值在于：

- 根据 `region` 对应的内部 `version-set` 拉到当前可见的 GAME release 列表

需要注意：

- 它不是完整历史库
- 通常只保留一个滚动窗口中的少量版本
- 因此 live 场景下 `STRICT` 经常失败，这是上游数据特性，不是 Resolver 自己制造的限制

### Inspector 的边界

Inspector 只根据 manifest 内容本身做判断：

- 它不替你验证上游 URL 是否“官方可信”
- 它不保证该 manifest 一定来自当前 live 区域
- 它只负责“看内容后得出类型与版本结论”

## 主要公开对象

### `VersionMatchMode`

匹配模式：

- `STRICT`
  - LCU 与 GAME 的 `normalized_build` 必须完全一致
- `IGNORE_REVISION`
  - 只要求同补丁，并选择“不高于 LCU build”的最大 GAME 候选
- `PATCH_LATEST`
  - 只要求同补丁，并直接选择当前可见的最新 GAME 候选

默认推荐：

- 资源场景优先用 `IGNORE_REVISION`
- 明确要“同补丁最新 GAME”时再用 `PATCH_LATEST`
- 只有做 EXE / DLL 严格对齐时才建议用 `STRICT`

### `VersionDisplayMode`

统一版本字符串的显示模式：

- `IGNORE_REVISION`
  - 默认，仅显示补丁号，例如 `16.5`
- `LCU`
  - 显示四段点分版本，例如 `16.5.751.8496`
- `GAME`
  - 显示三段紧凑版本，例如 `16.5.7511533`

### `VersionInfo`

表示单侧 manifest 的版本信息。

核心字段：

- `metadata_version`
- `exe_version`
- `normalized_build`
- `patch_version`

常用视图属性：

- `display_version`
- `compact_version`
- `dotted_version`

### `ManifestRef`

表示一个 manifest 引用。

核心字段：

- `artifact_group`
  - `lcu`、`game` 或 `unknown`
- `region`
  - Resolver 场景下是规范化后的用户区域，例如 `EUW` / `PBE`
  - Inspector 场景下固定为 `inspection`
- `source`
- `url`
- `manifest_id`
- `version`

### `ResolvedVersion`

统一版本对象。

它同时持有：

- `lcu: VersionInfo`
- `game: VersionInfo`
- `display_mode: VersionDisplayMode`

示例：

```python
print(str(pair.version))
print(pair.version.with_display_mode(VersionDisplayMode.LCU))
print(pair.version.with_display_mode(VersionDisplayMode.GAME))
```

### `ResolvedManifestPair`

这是当前主返回类型，表示“一对已经按规则配好的 LCU/GAME manifest”。

字段：

- `region`
- `version`
- `lcu`
- `game`
- `match_mode`
- `is_exact_match`
- `match_reason`
- `candidate_count`

#### `LiveManifestPair` 现在是什么？

`LiveManifestPair` 当前只是 `ResolvedManifestPair` 的兼容别名。

保留原因：

- 兼容旧调用方
- 避免 2.x 阶段直接打断已有项目

移除计划：

- 计划在 `v3.0.0` 删除

新代码请直接使用 `ResolvedManifestPair`。

## 主要错误类型

### 主错误名

- `LeagueManifestError`
- `RegionConfigNotFoundError`
- `LcuVersionUnavailableError`
- `ConsistentGameManifestNotFoundError`
- `ManifestInspectionError`

### 兼容错误名

以下名字当前仍保留，但都属于兼容层：

- `RiotGameDataError`
  - 兼容别名，等价于 `LeagueManifestError`
- `PatchlineConfigNotFoundError`
  - 兼容别名，等价于 `RegionConfigNotFoundError`
- `LiveConfigNotFoundError`
  - 兼容别名，等价于 `RegionConfigNotFoundError`

统一移除计划：

- 以上兼容层计划在 `v3.0.0` 删除

## `LeagueManifestResolver` 公开方法

### `available_regions()`

返回当前已识别的用户可见区域列表。

这是主方法。

说明：

- 返回值会优先使用规范化后的公开区域名，例如 `EUW`、`PBE`、`KR`
- 它不是 Riot 全部底层 ID 的原样列表

#### `available_lcu_regions()`

兼容包装，当前仍可用，但计划在 `v3.0.0` 删除。
新代码请改用 `available_regions()`。

### `get_lcu_manifest(region="EUW")`

返回指定区域当前对应的 LCU manifest。

适合：

- 你只关心 LCU，不关心 GAME

### `list_game_candidates(region="EUW")`

返回指定区域当前对应的 GAME manifest 候选列表。

说明：

- 这里的 `region` 仍然只传用户视角的区域
- 内部会自动解析到需要的 `version-set`

### `resolve_manifest_pair(...)`

签名重点：

```python
resolve_manifest_pair(
    region="EUW",
    match_mode=VersionMatchMode.IGNORE_REVISION,
    version_display_mode=VersionDisplayMode.IGNORE_REVISION,
)
```

这是 Resolver 的主入口。

说明：

- 只需要传一个 `region`
- 默认匹配模式已经是 `IGNORE_REVISION`
- 返回类型是 `ResolvedManifestPair`

### `resolve_version(...)`

如果你只关心统一版本号，而不关心 manifest URL，可以直接用这个方法。

### `build_lcu_extractor(region="EUW", ...)`

基于指定区域当前 LCU manifest 构造 `WADExtractor`。

### `build_game_extractor(region="EUW", ...)`

基于指定区域当前解析出的 GAME manifest 构造 `WADExtractor`。

### 兼容接口

以下接口当前仍保留，但都属于过渡层：

- `latest_lcu()`
- `latest_game()`
- `RiotGameData`

现状：

- 调用时会发出 `FutureWarning`
- 计划在 `v3.0.0` 删除

如果你在写新代码，请直接迁移到：

- `LeagueManifestResolver`
- `available_regions()`
- `resolve_manifest_pair()`
- `resolve_version()`

## `LeagueManifestInspector` 公开方法

### `inspect_manifest(source)`

输入一个 manifest 本地路径或 URL，返回：

- `ManifestRef`

用途：

- 看它是 `lcu`、`game` 还是 `unknown`
- 尽可能提取版本

### `inspect_pair(first, second, ...)`

输入两个 manifest，本方法会自动拆分出一 `lcu` + 一 `game`。

返回：

- `ResolvedManifestPair`

说明：

- 输入顺序不限
- Inspector 场景下 `candidate_count` 固定为 `1`
- 如果两侧版本不满足匹配规则，会抛出 `ConsistentGameManifestNotFoundError`

### `inspect_manifests(*sources, ...)`

- 传 `1` 个输入时返回 `ManifestRef`
- 传 `2` 个输入时返回 `ResolvedManifestPair`

## Inspector 的类型与版本提取逻辑

### 类型判定

- `game`
  - 命中 `content-metadata.json` 或 `League of Legends.exe`
- `lcu`
  - 命中 `LeagueClient.exe`
  - macOS 场景可回退到 `Contents/LoL/LeagueClient.app/Contents/Info.plist`
- `unknown`
  - 两侧强特征都不存在

如果同一个 manifest 同时命中 GAME 与 LCU 强特征，会抛出 `ManifestInspectionError`。

### 版本提取

#### GAME

优先顺序：

1. `content-metadata.json`
2. `League of Legends.exe`

#### LCU

优先顺序：

1. `LeagueClient.exe`
2. macOS `Info.plist`

## 匹配逻辑建议

### `STRICT`

适合：

- 校验 EXE / DLL 是否与目标 build 完全一致
- 把“精确 build 命中”当成业务前提

### `IGNORE_REVISION`

适合：

- 下载 WAD、语言包、贴图、音频等资源
- 接受“同补丁下 LCU 与 GAME 修订号不同”
- 仍然希望避免拿到高于当前 LCU 的 GAME build

### `PATCH_LATEST`

适合：

- 你明确希望拿到当前同补丁中最新的 GAME 候选
- 你接受该 GAME build 可能高于当前 LCU build

## 推荐调用方式

### 获取一对当前可用 manifest

```python
from riotmanifest import LeagueManifestResolver

resolver = LeagueManifestResolver()
pair = resolver.resolve_manifest_pair("EUW")

print(pair.region)
print(pair.lcu.url)
print(pair.game.url)
```

### 获取统一版本号

```python
from riotmanifest import LeagueManifestResolver

resolver = LeagueManifestResolver()
version = resolver.resolve_version("EUW")

print(str(version))
```

### 明确要求同补丁最新 GAME

```python
from riotmanifest import LeagueManifestResolver, VersionMatchMode

resolver = LeagueManifestResolver()
pair = resolver.resolve_manifest_pair(
    "PBE",
    match_mode=VersionMatchMode.PATCH_LATEST,
)
```

### 检查单个 manifest

```python
from riotmanifest import LeagueManifestInspector

inspector = LeagueManifestInspector()
manifest = inspector.inspect_manifest(
    "https://lol.secure.dyn.riotcdn.net/channels/public/releases/example.manifest"
)

print(manifest.artifact_group)
print(manifest.version.display_version if manifest.version else "unknown")
```

## 从旧接口迁移

旧写法通常会把用户心智撕裂成两套：

```python
resolver.load_lcu_data()
resolver.load_game_data(regions=["EUW1"])

lcu = resolver.latest_lcu("EUW")
game = resolver.latest_game("EUW1")
```

问题在于：

- 上层必须自己知道 `EUW` 和 `EUW1` 分别喂给谁
- 业务代码会直接暴露 Riot 的内部命名混乱
- 旧接口还会继续制造“LCU 区域”和“GAME version-set”是两套入参的错觉

推荐改成：

```python
from riotmanifest import LeagueManifestResolver

resolver = LeagueManifestResolver()
pair = resolver.resolve_manifest_pair("EUW")
```

如果旧代码传的是 `EUW1` / `PBE1`，当前也能继续工作；但新代码仍建议统一收口到 `EUW` / `PBE` 这样的主区域名。
