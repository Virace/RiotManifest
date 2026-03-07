# RiotManifest 测试与基准文档

以下命令均为模板写法，使用占位参数，不包含本机路径。

## 前置准备

```bash
uv sync
```

## Manifest 下载压力测试

```bash
RIOT_PERF_RUN=1 uv run pytest -q -s tests/test_manifest_download_speed.py
```

## 传输层对比测试

```bash
RIOT_TRANSPORT_BENCH_RUN=1 RIOT_TRANSPORT_MODE=both uv run pytest -q -s tests/test_downloader_transport_compare.py
```

## BIN 回填测试脚本

脚本：`scripts/test_wad_diff_with_bin_report.py`

```bash
uv run python scripts/test_wad_diff_with_bin_report.py \
  --old-manifest '<old_manifest_url_or_path>' \
  --new-manifest '<new_manifest_url_or_path>' \
  --flags '<locale_flag>' \
  --pattern '\\.wad\\.client$' \
  --bin-data-source-mode 'extractor' \
  --output-report 'out/manifest_diff_with_section_paths.json' \
  --output-wad-report 'out/wad_diff_with_section_paths_debug.json' \
  --output-timing 'out/wad_diff_with_section_paths_timing.json'
```

切换到整包下载模式：

```bash
--bin-data-source-mode 'download_root_wad'
```

## downloader 多轮基准脚本

脚本：`scripts/bench_downloader.py`

```bash
uv run python scripts/bench_downloader.py \
  '<manifest_url_or_path>' \
  --flag '<locale_flag>' \
  --pattern 'wad.client' \
  --concurrency 16 \
  --rounds 3 \
  --output-json 'out/downloader_bench_summary.json'
```

仅查看下载计划（dry-run）：

```bash
uv run python scripts/bench_downloader.py \
  '<manifest_url_or_path>' \
  --flag '<locale_flag>' \
  --pattern 'wad.client' \
  --concurrency 16 \
  --rounds 3 \
  --dry-run
```


## 真实 manifest 样本

开发期如果要手动验证 `LeagueManifestInspector` 或版本提取链路，可直接使用 `Morilli/riot-manifests`。
该仓库的 `LoL/<region>/<platform>/<artifact>/*.txt` 文件内容就是原始 manifest URL。

当前已验证的一组 EUW1 Windows 样本：

- `LoL/EUW1/windows/league-client/16.5.751.8496.txt`
- `LoL/EUW1/windows/lol-game-client/16.5.7511533.txt`

可用下面的 smoke 命令快速验证：

```bash
uv run python - <<'PY'
from riotmanifest import LeagueManifestInspector

inspector = LeagueManifestInspector()
pair = inspector.inspect_pair(
    "https://lol.secure.dyn.riotcdn.net/channels/public/releases/8E78E3C2EFDB30F0.manifest",
    "https://lol.secure.dyn.riotcdn.net/channels/public/releases/79CFFE595C2B0C01.manifest",
)
print(pair.match_reason)
print(pair.version.lcu.display_version)
print(pair.version.game.display_version)
PY
```
