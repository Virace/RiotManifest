# League Manifest 工具参考

## 作用范围

本文件说明 `LeagueManifestResolver`、`LeagueManifestInspector` 当前的定位、数据结构、匹配逻辑和对外版本对象。

范围说明：
- `riotmanifest.game` 当前只面向英雄联盟（LoL）清单
- 当前这两个类都不负责其他 Riot 游戏 manifest
- 其他游戏暂时没有接入计划

定义位置：
- [src/riotmanifest/game/factory.py](../src/riotmanifest/game/factory.py)
- [src/riotmanifest/game/inspection.py](../src/riotmanifest/game/inspection.py)
- [src/riotmanifest/game/metadata.py](../src/riotmanifest/game/metadata.py)

## 当前定位

### `LeagueManifestResolver`

`LeagueManifestResolver` 当前不是“完整历史版本管理器”。  
它的职责已经收敛为：

- 从 Riot 官方当前 patchline 配置中定位 LCU manifest
- 找到当前 patchline 对应的 GAME 候选集合
- 按规则构造“当前 patchline 且版本规则明确”的一对 LCU/GAME manifest
- 提供统一版本号对象

### `LeagueManifestInspector`

`LeagueManifestInspector` 的职责与 live patchline 无关。
它的定位是：

- 从单个 manifest 文件或 URL 出发，识别它是 `lcu`、`game` 还是 `unknown`
- 在单清单场景返回类型与版本信息
- 在双清单场景自动尝试配对；当结果满足 `VersionMatchMode` 时返回 `LiveManifestPair`

## 兼容说明

- `RiotGameData` 仍保留为兼容旧名，实例化时会发出 `FutureWarning`。
- 旧类名计划在 `v3.0.0` 删除；新代码请直接使用 `LeagueManifestResolver`。

## 数据源逻辑

### `clientconfig.rpg.riotgames.com`

用途：

- 提供当前 live / pbe patchline 配置

当前可用核心字段：

- `patch_artifacts.league_client.patch_url`
- `patch_artifacts.game_client.patchsieve`

当前不可依赖字段：

- `secondary_patchlines.game_patch`
  - 已证实为陈旧值
- `metadata.theme_manifest`
  - 只能作为版本提示，不是严格版本依据

### `sieve.services.riotcdn.net`

用途：

- 提供某个 `version_set` 下的 GAME release 候选列表

特点：

- 往往保留当前版本和少量上一个版本
- 不是完整历史数据库

## 核心对象

### `VersionMatchMode`

匹配模式：

- `STRICT`
  - LCU 与 GAME 的 `normalized_build` 必须完全一致
- `IGNORE_REVISION`
  - 只要求 `patch_version` 一致，并优先选“不高于 LCU build”的最大 GAME 候选
- `PATCH_LATEST`
  - 只要求 `patch_version` 一致，并直接选同补丁内最新的 GAME 候选

### `VersionDisplayMode`

统一版本号字符串的显示模式：

- `IGNORE_REVISION`
  - 默认，输出补丁号，例如 `16.5`
- `LCU`
  - 输出四段点分版本，例如 `16.5.751.1533`
- `GAME`
  - 输出三段紧凑版本，例如 `16.5.7511533`

### `VersionInfo`

表示单侧版本信息。

字段：

- `metadata_version`
  - metadata 来源的三段版本号，例如 `16.5.7511533`
  - LCU 当前通常为 `None`
- `exe_version`
  - exe 来源的四段版本号，例如 `16.5.751.1533`
- `normalized_build`
  - 统一比较值，例如 `16.5.7511533`
- `patch_version`
  - 统一补丁号，例如 `16.5`

兼容与视图属性：

- `display_version`
  - 兼容旧接口的默认显示值
  - 有 `metadata_version` 时优先显示三段版本，否则显示四段 exe 版本
- `compact_version`
  - 三段紧凑版本号，例如 `16.5.7511533`
- `dotted_version`
  - 四段点分版本号，例如 `16.5.751.1533`

示例：

- LCU
  - `metadata_version = None`
  - `exe_version = "16.5.751.1533"`
  - `normalized_build = "16.5.7511533"`
  - `patch_version = "16.5"`
- GAME（metadata 路径）
  - `metadata_version = "16.5.7511533"`
  - `exe_version = None`
  - `normalized_build = "16.5.7511533"`
  - `patch_version = "16.5"`

### `ManifestRef`

表示一个 manifest 引用。

字段：

- `artifact_group`
  - `lcu`、`game` 或 `unknown`
- `region`
  - Inspector 场景固定为 `inspection`
- `source`
  - 例如 `clientconfig` / `sieve` / `manifest_inspector`
- `url`
- `manifest_id`
- `version`

### `ResolvedVersion`

这是当前对外最重要的“统一版本号对象”。

字段：

- `lcu: VersionInfo`
- `game: VersionInfo`
- `display_mode: VersionDisplayMode`

行为：

- `str(resolved_version)` 会按当前显示模式输出字符串
- 默认输出补丁号
- 可通过 `with_display_mode(...)` 切换输出方式

示例：

```python
print(str(pair.version))  # 16.5
print(pair.version.with_display_mode(VersionDisplayMode.LCU))   # 16.5.751.1533
print(pair.version.with_display_mode(VersionDisplayMode.GAME))  # 16.5.7511533
```

### `LiveManifestPair`

表示当前 live 且版本规则明确的一对结果。

字段：

- `region`
- `version`
- `lcu`
- `game`
- `match_mode`
- `is_exact_match`
- `match_reason`
- `candidate_count`

理解方式：

- `lcu` / `game`
  - 是具体 manifest
- `version`
  - 是对外版本号对象
- `is_exact_match`
  - 表示是否 build 级完全一致
- `match_reason`
  - 解释这次结果是怎么选出来的

## `LeagueManifestInspector` 公开方法

### `inspect_manifest(source)`

返回：

- `ManifestRef`

用途：

- 从单个 manifest 文件或 URL 出发，识别它是 `lcu`、`game` 还是 `unknown`
- 如果能从内容中提取版本，则把结果填入 `manifest.version`

### `inspect_pair(first, second, ...)`

签名重点：

```python
inspect_pair(
    first,
    second,
    match_mode=VersionMatchMode.IGNORE_REVISION,
    version_display_mode=VersionDisplayMode.IGNORE_REVISION,
)
```

返回：

- `LiveManifestPair`

行为：

- 输入顺序不限；内部会自动拆出一 `lcu` + 一 `game`
- 两侧都带版本且满足 `VersionMatchMode` 时才返回结果
- Inspector 场景的 `candidate_count` 固定为 `1`

### `inspect_manifests(*sources, ...)`

返回：

- 传入 `1` 个输入时返回 `ManifestRef`
- 传入 `2` 个输入时返回 `LiveManifestPair`

适合：

- 你希望让上层代码直接按输入数量分发，而不手动判断调用哪个方法

## Inspector 判定与版本提取逻辑

### 类型判定

- `GAME`
  - 命中 `content-metadata.json` 或 `League of Legends.exe`
- `LCU`
  - 命中 `LeagueClient.exe`
  - macOS 场景可次级回退到 `Contents/LoL/LeagueClient.app/Contents/Info.plist`
- `unknown`
  - 两侧强特征都不存在时返回 `unknown`

### 版本提取

- `GAME`
  - 优先读取 `content-metadata.json`
  - 若缺失，再回退 `League of Legends.exe`
- `LCU`
  - 优先读取 `LeagueClient.exe`
  - macOS 仅作次级支持
- `system.yaml`
  - 不属于 Inspector 的版本提取路径
  - Resolver 内部残留的弱提示逻辑也不应作为对外依赖

### 信任边界

`LeagueManifestInspector` 是开发者工具。

它只根据 manifest 内容提取类型与版本，不对上游 manifest 的真实性做额外背书。
如果上游项目提供的是伪造或被篡改的 manifest，那么 Inspector 返回的类型和版本同样不可信。

### 真实样本来源

手动验证时，可直接使用 `Morilli/riot-manifests` 中的 LoL 样本。
该仓库当前按 `LoL/<region>/<platform>/<artifact>/...` 组织，其中 `.txt` 文件内容就是原始 manifest URL。

例如当前可直接配对的一组 EUW1 Windows 样本：

- `LoL/EUW1/windows/league-client/16.5.751.8496.txt`
- `LoL/EUW1/windows/lol-game-client/16.5.7511533.txt`

## 错误对象

### `RiotGameDataError`

`RiotGameDataError` 当前仍作为兼容错误基类保留。

### `ManifestInspectionError`

表示 Inspector 无法从输入中形成稳定结论。
常见原因包括：

- 同一个 manifest 同时命中 LCU / GAME 强特征
- 两个输入无法组成“一 LCU 一 GAME”
- 期望的版本载体不存在或内容不可解析

### `LiveConfigNotFoundError`

表示目标区域不存在可用 live 配置，或者 live 配置缺少必要字段。

### `LcuVersionUnavailableError`

表示无法从 LCU manifest 中严格提取版本。

### `ConsistentGameManifestNotFoundError`

表示当前 GAME 候选集合中，找不到满足匹配规则的版本。

## `LeagueManifestResolver` 公开方法

### `get_lcu_manifest(region="EUW")`

返回：

- `ManifestRef`

用途：

- 只拿当前 patchline 的 LCU manifest，不关心 GAME

### `list_game_candidates(region="EUW")`

返回：

- `list[ManifestRef]`

用途：

- 只看当前 patchline 对应的 GAME 候选集合

### `resolve_manifest_pair(...)`

签名重点：

```python
resolve_manifest_pair(
    region="EUW",
    match_mode=VersionMatchMode.IGNORE_REVISION,
    version_display_mode=VersionDisplayMode.IGNORE_REVISION,
)
```

返回：

- `LiveManifestPair`

这是主入口。

注意：

- 方法签名的默认值现在就是 `IGNORE_REVISION`。
- 如果你只处理资源文件，大多数情况下可以直接不传 `match_mode`。
- 原因是 Riot live 经常先推进 GAME，再稍后推进 LCU；而 `patchsieve` 只保留当前滚动窗口中的少量 GAME 候选，不是完整历史库。

### `resolve_version(...)`

签名重点：

```python
resolve_version(
    region="EUW",
    match_mode=VersionMatchMode.IGNORE_REVISION,
    display_mode=VersionDisplayMode.IGNORE_REVISION,
)
```

返回：

- `ResolvedVersion`

适合：

- 你只关心版本号，不关心 manifest URL

### `build_lcu_extractor(region="EUW", ...)`

基于当前 live 的 LCU manifest 构造 `WADExtractor`。

### `build_game_extractor(region="EUW", ...)`

基于当前 live 且匹配规则明确的 GAME manifest 构造 `WADExtractor`。

注意：

- 这里输入的是 LCU live 区域，例如 `EUW`
- 不再推荐外部直接把 `EUW1` 当主入口区域

## 版本解析逻辑

### LCU 版本

默认不再用 `theme_manifest` 推导精确版本。  
当前严格版本提取流程是：

1. 下载 `league_client.patch_url` 对应的 manifest
2. 优先查找 `LeagueClient.exe`
3. 从其 UTF-16LE 版本资源中提取：
   - `ProductVersion`
   - `FileVersion`
4. macOS 场景才回退到 `Info.plist`
5. `system.yaml` 的旧提示路径不属于对外严格解析能力

### GAME 版本

- `LeagueManifestResolver`
  - 当前来自 `patchsieve.version_set` 对应的 `lol-game-client` release 列表
- `LeagueManifestInspector`
  - 优先读取 `content-metadata.json`
  - 若缺失，再回退 `League of Legends.exe`

## 匹配逻辑

### `STRICT`

规则：

- `lcu.version.normalized_build == game.version.normalized_build`
- `metadata_version` 与 `exe_version` 只是来源/格式视图，严格比较统一看 `normalized_build`

找不到就失败，不做隐式回退。

这是“精确 build 校验模式”，不是 live 资源拉取场景的默认推荐模式。

它在 live 场景里大概率失败，常见原因是：

- GAME 修订号往往会先于 LCU 前进
- `patchsieve` 不保证保留与当前 LCU 完全同 build 的 GAME 条目

一个实测样例（EUW，2026-03-07）：

- LCU：`16.5.751.8496`
- API 当前可见的 GAME 候选：`16.5.7496037`、`16.5.7511533`、`16.5.7519084`
- 结果：不存在完全一致的 `16.5.7518496`，因此 `STRICT` 直接失败

### `IGNORE_REVISION`

规则：

- 只要求 `lcu.version.patch_version == game.version.patch_version`
- 若同补丁下有多个候选，优先取“不高于 LCU build”的最大 GAME 版本
- 若同补丁候选全部高于 LCU build，则直接失败

这是本文档在 live 场景中的默认推荐模式。

这个模式适合：

- 同补丁内存在 exe / dll 小修订
- GAME 修订号快于 LCU，但你仍需要拿当前 live 对应资源
- 资源文件通常未跟着变化

实测样例（EUW，2026-03-07）：

- LCU：`16.5.751.8496`
- GAME 候选：`16.5.7496037`、`16.5.7511533`、`16.5.7519084`
- 当前逻辑会选择：`16.5.7511533`
- 原因：它是同补丁内“最大且不高于 LCU”的 GAME build

如果你只处理这些内容，通常可以忽略修订号：

- `wad.client`
- 语言包
- 贴图
- 音频
- 其他资源侧文件

### `PATCH_LATEST`

规则：

- 只要求 `lcu.version.patch_version == game.version.patch_version`
- 若同补丁下有多个候选，直接取最新的 GAME 版本

这个模式适合：

- 你明确接受“GAME 修订号可以高于 LCU”
- 你只想拿到当前 patchsieve 暴露的同补丁最新 GAME 候选

实测样例（EUW，2026-03-07）：

- LCU：`16.5.751.8496`
- GAME 候选：`16.5.7496037`、`16.5.7511533`、`16.5.7519084`
- `PATCH_LATEST` 会选择：`16.5.7519084`
- `IGNORE_REVISION` 会选择：`16.5.7511533`

只有在你明确要做这些事情时，才更适合使用 `STRICT`：

- 校验 EXE / DLL 是否与目标 build 完全一致
- 分析 code-side 二进制修订差异
- 把“精确 build 命中”本身当作业务前提

## 强烈建议（live 资源场景）

- `LeagueManifestResolver` 现在默认就使用 `VersionMatchMode.IGNORE_REVISION`。
- 如果你的目标是下载 WAD、语言包、贴图、音频等资源，请优先使用 `IGNORE_REVISION`。
- 如果你明确要“同补丁下最新 GAME”，再显式切到 `PATCH_LATEST`。
- 如果你坚持使用 `STRICT`，应预期它在 live 窗口期经常抛出 `ConsistentGameManifestNotFoundError`。

## 推荐调用方式

### 拿当前 live 且版本一致的一对 URL

```python
from riotmanifest import LeagueManifestResolver

resolver = LeagueManifestResolver()
pair = resolver.resolve_manifest_pair("EUW")

print(pair.lcu.url)
print(pair.game.url)
```

### 拿统一版本号

```python
from riotmanifest import LeagueManifestResolver

resolver = LeagueManifestResolver()
version = resolver.resolve_version("EUW")
print(str(version))  # 16.5
```

### 切换版本号显示模式

```python
from riotmanifest import LeagueManifestResolver, VersionDisplayMode

resolver = LeagueManifestResolver()
version = resolver.resolve_version("EUW")

print(version.with_display_mode(VersionDisplayMode.LCU))
print(version.with_display_mode(VersionDisplayMode.GAME))
```

### 默认推荐：使用默认匹配模式

```python
from riotmanifest import LeagueManifestResolver

resolver = LeagueManifestResolver()
pair = resolver.resolve_manifest_pair("EUW")
```

### 如果你要同补丁里的最新 GAME

```python
from riotmanifest import LeagueManifestResolver, VersionMatchMode

resolver = LeagueManifestResolver()
pair = resolver.resolve_manifest_pair(
    "EUW",
    match_mode=VersionMatchMode.PATCH_LATEST,
)
```

### 如果你必须使用 `STRICT`

```python
from riotmanifest import (
    ConsistentGameManifestNotFoundError,
    LeagueManifestResolver,
    VersionMatchMode,
)

resolver = LeagueManifestResolver()

try:
    pair = resolver.resolve_manifest_pair(
        "EUW",
        match_mode=VersionMatchMode.STRICT,
    )
except ConsistentGameManifestNotFoundError:
    # live 窗口期这里大概率会触发
    pair = None
```

### 判断单个 manifest 的类型与版本

```python
from riotmanifest import LeagueManifestInspector

inspector = LeagueManifestInspector()
manifest = inspector.inspect_manifest(
    "https://lol.secure.dyn.riotcdn.net/channels/public/releases/79CFFE595C2B0C01.manifest"
)

print(manifest.artifact_group)           # game
print(manifest.version.display_version)  # 16.5.7511533
```

### 判断两个 manifest 能否组成一对

```python
from riotmanifest import LeagueManifestInspector

inspector = LeagueManifestInspector()
pair = inspector.inspect_pair(
    "https://lol.secure.dyn.riotcdn.net/channels/public/releases/8E78E3C2EFDB30F0.manifest",
    "https://lol.secure.dyn.riotcdn.net/channels/public/releases/79CFFE595C2B0C01.manifest",
)

print(str(pair.version))  # 16.5
print(pair.lcu.version.display_version)   # 16.5.751.8496
print(pair.game.version.display_version)  # 16.5.7511533
```

## 从旧接口迁移

旧调用方式通常是：

```python
resolver.load_lcu_data()
resolver.load_game_data(regions=["EUW1"])

lcu = resolver.latest_lcu("EUW")
game = resolver.latest_game("EUW1")
```

问题：

- 两个 `latest` 的语义不同
- 补丁窗口期内天然可能不一致
- 这两个兼容接口当前调用时会发出 `FutureWarning`
- 计划在 `v3.0.0` 删除

推荐迁移为：

```python
from riotmanifest import LeagueManifestResolver, VersionMatchMode

resolver = LeagueManifestResolver()
pair = resolver.resolve_manifest_pair(
    "EUW",
    match_mode=VersionMatchMode.IGNORE_REVISION,
)
```

如果只想取版本号：

```python
from riotmanifest import LeagueManifestResolver, VersionMatchMode

resolver = LeagueManifestResolver()
version = resolver.resolve_version(
    "EUW",
    match_mode=VersionMatchMode.IGNORE_REVISION,
)
```
