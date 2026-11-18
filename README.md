# open-orange-config-gate

Static preflight for [OpenOrange](https://github.com/rivet-dev/open-orange): point it at the Wrangler
configs you already deploy, and it tells you which declared bindings **cannot work** under `orange dev`,
whether they fail by throwing or by **silently dropping data**, and which config keys stop the Worker from
starting at all — before you spend a migration window discovering it at runtime.

Why now: OpenOrange is a fast-moving Cloudflare Workers runtime (660★, pushed 2026-11-17) whose support
matrix lives in a docs table. `wrangler dev` accepts every binding; `orange dev` accepts the config and
then either throws per call or, for Analytics Engine, drops every data point without an error.

Upstream: https://github.com/rivet-dev/open-orange

## What the upstream does, and what it does not do

OpenOrange runs Cloudflare Workers on Node.js/Rivet and reads the same Wrangler configuration
(`docs/modules/bindings.mdx` L8). It documents a support matrix: drop-in, pluggable, partial, and
not supported. For example:

- Analytics Engine is "Not supported", and `writeDataPoint()` **drops each data point** (README L168).
- Vectorize, Images, Media, Stream, Pipelines, AI Search, mTLS certificates, VPC and legacy blobs are
  "Not supported", and **calls throw** (README L169).
- Ten configuration keys stop `orange dev` outright: `cloudchamber`, `connect`, `define`, `exports`,
  `legacy_assets`, `pages_build_output_dir`, `site`, `streaming_tail_consumers`, `triggers.events`, and
  any `unsafe` other than `unsafe.bindings` (bindings.mdx L163–L169).

What upstream does **not** ship is anything that answers the question a migrating team actually has:
*for these specific config files, which Workers are affected, how badly, and in which failure mode?*
You have to read the table, enumerate bindings by hand, and know that Wrangler env blocks inherit
top-level bindings — including the ones that will not work.

Evidence links (pinned to `e5e795446d2b8d9e7be289d6fbc6f87630e1ef34`):

- <https://github.com/rivet-dev/open-orange/blob/e5e795446d2b8d9e7be289d6fbc6f87630e1ef34/docs/modules/bindings.mdx#L130>
- <https://github.com/rivet-dev/open-orange/blob/e5e795446d2b8d9e7be289d6fbc6f87630e1ef34/docs/modules/bindings.mdx#L137>
- <https://github.com/rivet-dev/open-orange/blob/e5e795446d2b8d9e7be289d6fbc6f87630e1ef34/README.md#L168>

## What this tool does / does not do

Does:

- Parses `wrangler.jsonc`, `wrangler.json` and `wrangler.toml` (JSONC comments and trailing commas included).
- Resolves Wrangler's top-level → `env.<name>` inheritance (arrays are replaced, not merged; `name`, `main`,
  `compatibility_date` and similar are not inherited) so each environment is judged on its *effective* config.
- Classifies every declared binding into `drop_in` / `partial` / `needs_service` / `not_supported`, with a
  **failure mode**: `none`, `degraded`, `throws_until_configured`, `throws`, or `silent_drop`.
- Reports config keys that stop `orange dev`, and keys accepted only with a warning (`containers`).
- Optionally cross-checks `env.<NAME>` references in a source tree (`--src`) against declared bindings, so a
  typo'd or undeclared binding is caught statically.
- Emits a stable `input_sha256` over the exact config bytes, so two runs are comparable.

Does not:

- Run `orange dev` or make network calls; it does not prove runtime behaviour, only config-level exposure.
- Parse TypeScript, evaluate code, or see bindings created outside Wrangler config.
- Track upstream after the pinned commit. The matrix is a snapshot, not a live feed.

## Install

Zero dependencies, Python 3.11+ (uses `tomllib`). Nothing to install:

```bash
git clone git@github-linmoxi33:linmoxi33/open-orange-config-gate.git
cd open-orange-config-gate
python3 -m unittest discover -s tests          # 27 tests
```

## Usage

First command to try (clean config → exit 0):

```bash
python3 bin/open_orange_config_gate.py check examples/in/clean --json
```

A config that will lose data (exit 1):

```bash
python3 bin/open_orange_config_gate.py check examples/in/risky --src examples/in/risky/src
```

```
BLOCKER shop-api/production L6 [ANALYTICS] binding `ANALYTICS` (analytics_engine_datasets) is not supported: writeDataPoint() drops each data point without raising, so the Worker appears healthy while losing all writes
BLOCKER shop-api/production L7 [INDEX] binding `INDEX` (vectorize) is not supported: calls throw at runtime
WARNING shop-api/production L8 `containers` is accepted but only with a warning under `orange dev`
WARNING shop-api-edge/edge L13 [AI] binding `AI` (ai) needs a configured provider; ...
summary: 2 blocker(s), 3 warning(s), 1 silent-data-loss item(s), 3 worker env(s)
```

`--json` output (trimmed real run, see `examples/out/risky.json` for the full document):

```json
{
  "schema_version": 1,
  "tool": "open-orange-config-gate",
  "pinned_commit": "e5e795446d2b8d9e7be289d6fbc6f87630e1ef34",
  "input_sha256": "…",
  "workers": [
    {
      "name": "shop-api",
      "environment": "production",
      "findings": [
        {
          "code": "SILENT_DATA_LOSS_RISK",
          "severity": "blocker",
          "status": "not_supported",
          "failure_mode": "silent_drop",
          "binding": "ANALYTICS",
          "key": "analytics_engine_datasets",
          "line": 6,
          "evidence": "https://github.com/rivet-dev/open-orange/blob/e5e7954…/docs/modules/bindings.mdx#L130",
          "referenced_at": {"file": "src/index.ts", "line": 5}
        }
      ]
    }
  ],
  "summary": {"blockers": 2, "warnings": 3, "silent_data_loss": 1, "workers": 3}
}
```

Exit codes:

| code | meaning |
| --- | --- |
| 0 | no blockers (warnings may exist) |
| 1 | at least one blocker; with `--fail-on any`, any warning also fails |
| 2 | usage error, unreadable path, or unparsable config |

Options: `--json`, `--src DIR`, `--fail-on blocker|any`. `matrix` subcommand prints the pinned matrix
as JSON with an evidence link per binding.

## 本轮创新

首次把 OpenOrange 的支持矩阵 + “会停止启动的配置键” 编译成**对真实多 worker/多 env Wrangler 配置的可判定门禁**：
按 Wrangler 的环境继承规则展开每个 env 的实际绑定，并把失败模式分成「静默丢数据 / 调用抛错 / 缺 provider 才抛错」，
让最危险的一类（Analytics Engine `writeDataPoint()` 静默丢点）排在硬报错之前。上游与其文档只给出静态表格，
没有把表格落到具体配置、具体行号、具体 env 的工具；同类检索未见专门做 OpenOrange 迁移前置检查的仓库
（详见交付记录中的检索命令与结果）。

## 与同类项目的差异

- 通用 Workers 兼容性检查器面向 Cloudflare 官方 `workerd`/`wrangler`，不覆盖 OpenOrange 的「Not supported」
  与「Needs a service」两类差异；本工具的矩阵逐条绑定到 OpenOrange 文档行号。
- 文档表只给状态；本工具额外给出：环境继承后的实际暴露面、配置级启动阻断键、源码 `env.<NAME>` 引用交叉核对、
  以及可复现的 `input_sha256`。

## Limitations

- The status matrix is pinned to upstream commit `e5e7954` (2026-11-17). If OpenOrange changes support,
  this tool is stale until the table is updated; `pinned_commit` in the output makes that explicit.
- `line` is the first line where the config key appears, not a full JSON pointer; duplicate keys in
  different blocks can share a line number.
- `--src` is a regex scan (`env.<NAME>`, `writeDataPoint(`), not a TypeScript parse; destructuring
  (`const { KV } = env`) and aliases are not followed. Findings from it are warnings, never blockers.
- Inherited env blocks are only reported once (under `production`); per-env findings appear only when the
  env block overrides or adds the key.
- `unsafe` bindings other than `unsafe.bindings` are documented as unsupported upstream, but this tool does
  not classify arbitrary `unsafe` payloads; it checks the named config keys only.

## License

MIT. See `LICENSE`. Upstream OpenOrange is Apache-2.0; this repository contains no upstream code.