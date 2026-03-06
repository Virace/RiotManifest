# RiotGameData 参考

## 作用范围

本文件说明 `RiotGameData` 当前的定位、数据结构、匹配逻辑和对外版本对象。

定义位置：
- [src/riotmanifest/game/factory.py](../src/riotmanifest/game/factory.py)
- [src/riotmanifest/game/metadata.py](../src/riotmanifest/game/metadata.py)

## 当前定位

`RiotGameData` 当前不是“完整历史版本管理器”。  
它的职责已经收敛为：

- 从 Riot 官方当前 live 配置中定位 LCU manifest
- 找到当前 live 对应的 GAME 候选集合
- 按规则构造“当前 live 且版本规则明确”的一对 LCU/GAME manifest
- 提供统一版本号对象

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
  - 只要求 `patch_version` 一致

### `VersionDisplayMode`

统一版本号字符串的显示模式：

- `IGNORE_REVISION`
  - 默认，输出补丁号，例如 `16.5`
- `LCU`
  - 输出 LCU 显示版本，例如 `16.5.751.1533`
- `GAME`
  - 输出 GAME 显示版本，例如 `16.5.7511533`

### `VersionInfo`

表示单侧版本信息。

字段：

- `display_version`
- `normalized_build`
- `patch_version`

示例：

- LCU
  - `display_version = "16.5.751.1533"`
  - `normalized_build = "16.5.7511533"`
  - `patch_version = "16.5"`
- GAME
  - `display_version = "16.5.7511533"`
  - `normalized_build = "16.5.7511533"`
  - `patch_version = "16.5"`

### `ManifestRef`

表示一个 manifest 引用。

字段：

- `artifact_group`
  - `lcu` 或 `game`
- `region`
- `source`
  - 例如 `clientconfig` / `sieve`
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

## 错误对象

### `RiotGameDataError`

`RiotGameData` 相关错误基类。

### `LiveConfigNotFoundError`

表示目标区域不存在可用 live 配置，或者 live 配置缺少必要字段。

### `LcuVersionUnavailableError`

表示无法从 live LCU manifest 中严格提取版本。

### `ConsistentGameManifestNotFoundError`

表示当前 GAME 候选集合中，找不到满足匹配规则的版本。

## `RiotGameData` 公开方法

### `get_live_lcu_manifest(region="EUW")`

返回：

- `ManifestRef`

用途：

- 只拿当前 live LCU manifest，不关心 GAME

### `list_live_game_candidates(region="EUW")`

返回：

- `list[ManifestRef]`

用途：

- 只看当前 live 对应的 GAME 候选集合

### `resolve_live_manifest_pair(...)`

签名重点：

```python
resolve_live_manifest_pair(
    region="EUW",
    match_mode=VersionMatchMode.STRICT,
    version_display_mode=VersionDisplayMode.IGNORE_REVISION,
)
```

返回：

- `LiveManifestPair`

这是主入口。

### `resolve_live_version(...)`

签名重点：

```python
resolve_live_version(
    region="EUW",
    match_mode=VersionMatchMode.STRICT,
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
4. 若是 macOS 路径，则回退到 `Info.plist`
5. 若只能拿到 `system.yaml` 的 `Releases/16.5` 这种提示，则视为“不足以严格解析”

### GAME 版本

当前来自 `patchsieve.version_set` 对应的 `lol-game-client` release 列表。

## 匹配逻辑

### `STRICT`

规则：

- `lcu.version.normalized_build == game.version.normalized_build`

找不到就失败，不做隐式回退。

### `IGNORE_REVISION`

规则：

- 只要求 `lcu.version.patch_version == game.version.patch_version`
- 若同补丁下有多个候选，取最大版本

这个模式适合：

- 同补丁内存在 exe / dll 小修订
- 资源文件通常未跟着变化

## 推荐调用方式

### 拿当前 live 且版本一致的一对 URL

```python
from riotmanifest import RiotGameData

rgd = RiotGameData()
pair = rgd.resolve_live_manifest_pair("EUW")

print(pair.lcu.url)
print(pair.game.url)
```

### 拿统一版本号

```python
from riotmanifest import RiotGameData

rgd = RiotGameData()
version = rgd.resolve_live_version("EUW")
print(str(version))  # 16.5
```

### 切换版本号显示模式

```python
from riotmanifest import RiotGameData, VersionDisplayMode

rgd = RiotGameData()
version = rgd.resolve_live_version("EUW")

print(version.with_display_mode(VersionDisplayMode.LCU))
print(version.with_display_mode(VersionDisplayMode.GAME))
```

### 放宽到忽略修订号

```python
from riotmanifest import RiotGameData, VersionMatchMode

rgd = RiotGameData()
pair = rgd.resolve_live_manifest_pair(
    "EUW",
    match_mode=VersionMatchMode.IGNORE_REVISION,
)
```

## 从旧接口迁移

旧调用方式通常是：

```python
rgd.load_lcu_data()
rgd.load_game_data(regions=["EUW1"])

lcu = rgd.latest_lcu("EUW")
game = rgd.latest_game("EUW1")
```

问题：

- 两个 `latest` 的语义不同
- 补丁窗口期内天然可能不一致

推荐迁移为：

```python
pair = rgd.resolve_live_manifest_pair("EUW")
```

如果只想取版本号：

```python
version = rgd.resolve_live_version("EUW")
```
