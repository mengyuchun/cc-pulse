<div align="center">

# CC-Pulse

**Listen to the "heartbeat" of [cc-switch](https://github.com/farion1231/cc-switch) providers — health checks and deep single-model diagnostics**

Don't trust "it connected". Trust "it works". With so many providers, see at a glance which ones you can actually use.

[中文](README.md) · English

[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/stdlib%20only-green.svg)](#)
[![Tests](https://img.shields.io/badge/tests-659%20pass-brightgreen.svg)](#tests)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

</div>

---

## TL;DR

```bash
git clone https://github.com/mengyuchun/cc-pulse.git
cd cc-pulse
python check_ccswitch_health.py check --failover-only   # daily check, fastest
```

If you have [just](https://just.systems/) installed, one line replaces all three: `just check`.

> Want to deep-dive on a single model afterwards: `python check_ccswitch_health.py inspect --provider "Relay-A" --model "claude-sonnet-4-6" --human`

---

## Glossary

| Term | Meaning |
|------|---------|
| **cc-switch** | Desktop tool that manages many Claude Code / Codex / OpenClaw relay providers; keeps a `cc-switch.db` |
| **Relay** | Third-party service forwarding LLM API requests — often silently rewrites routing, model, or auth |
| **Failover queue** | The list of providers cc-switch falls back through; `--failover-only` probes only those + the active one |
| **Silent routing** | You picked A but traffic went elsewhere — typically because a terminal env var (`ANTHROPIC_BASE_URL` etc.) overrides cc-switch's choice |
| **Tier** | The `haiku → sonnet → opus → fable → default` model-priority fallback order; CC-Pulse stops at the first tier that answers correctly |

---

## Why CC-Pulse

cc-switch helps you manage many Claude Code / Codex API relay providers. But relays are messier than they look:

- 🔇 **200 ≠ usable**: some providers return 200 with empty answers, wrong answers, or silently route to cheaper models
- 🎭 **Tier traps**: haiku works, sonnet fails, opus is rate-limited, fable does not exist
- 🔑 **Auth styles differ**: some accept only `x-api-key`, some only `Authorization: Bearer`, some validate client User-Agent
- 🧠 **Thinking models**: DeepSeek / GLM may burn a 20-token budget on thinking and return no final answer
- 📏 **Shrinking context**: advertised as 1M, rejected around 526k
- 🛠️ **Tool / vision gaps**: coding agents depend on `tool_use`, but many relays only pretend to support it

CC-Pulse does not stop at connectivity. It checks whether a provider can actually be used — so with so many providers, you can see at a glance which ones really work.

---

## How it differs from cc-switch's built-in checks

cc-switch includes stream / health monitoring and records fields such as `http_status` / `response_time_ms` / `success`. CC-Pulse is **complementary**, not a replacement: cc-switch manages configuration and switching; CC-Pulse focuses on deep probing.

| Dimension | cc-switch built-in checks | CC-Pulse |
|-----------|---------------------------|----------|
| Focus | Connectivity / latency / runtime state | Real usability (auth + correct answers) |
| Sends real model requests upstream | Depends on current cc-switch version and check config | ✅ Real request for every tier |
| Invalid API key / token | Depends on whether the check covers that provider's auth path | ✅ 401 / 403 clearly classified as `authentication` |
| HTTP 200 with empty answer | May not be distinguishable from a healthy connect | ✅ `answer_mismatch` (unusable) |
| HTTP 200 with business-error body | May look like "HTTP success" | ✅ `invalid_response`, keeps original error text |
| Thinking burns all tokens | Outside basic connectivity scope | ✅ Thinking disabled by default; adjustable `max_tokens` |
| Silent model routing | ❌ Outside basic check scope | ✅ `inspect` compares request / response model |
| Multi-tier fallback | Depends on runtime failover | ✅ Active probe: haiku → sonnet → opus → fable → default |
| Streaming / tools / context / vision | ❌ Outside basic check scope | ✅ 7-dimension `inspect` diagnostics |

**Typical trap scenarios** (all seen in real use):

**① HTTP 200, but body is a business error**

```json
{"code":0,"msg":"legacy forwarding path closed","data":null}
```

✅ Connected · ❌ No model content → CC-Pulse classifies as `invalid_response`

**② HTTP 200, but answer is empty**

```json
{"content":[{"type":"text","text":""}]}
```

✅ Connected · ❌ Thinking burned the budget, no final answer → CC-Pulse classifies as `answer_mismatch`

**③ Key / token invalid or revoked**

```json
{"type":"error","error":{"type":"AuthError","message":"Invalid API key."}}
```

✅ Endpoint is alive · ❌ Auth failed, unusable in practice → CC-Pulse classifies as `authentication` and shows where it failed

**④ Key can list models but cannot complete inference**

```text
GET /v1/models  → 200 ✅
POST /v1/messages → 401 Invalid API key ❌
```

If a basic check only covers connectivity, it may treat "models list works" as healthy. CC-Pulse sends a real inference request and exposes the second-half auth failure.

In one line: **cc-switch answers "can it connect?"; CC-Pulse answers "can I use it?"**

## Core features

### 1. Health check `check` — multi-tier fallback + real answer verification

- Probes in order `haiku → sonnet → opus → fable → default`, **stops at the first tier that answers correctly**
- Randomly picks a question from a pool (arithmetic, mixed EN/中文) and validates the answer leniently — **HTTP 200 ≠ usable**. The rotating pool + natural `max_tokens` (1024) makes probes look less like a fixed liveness-check script
- Auth follows cc-switch config: `ANTHROPIC_AUTH_TOKEN` → `Bearer`, `ANTHROPIC_API_KEY` → `x-api-key`
- **Live progress**: one line as soon as each tier finishes — no waiting until everything ends
- Concurrent batching + full error text passthrough
- **Stealth mode `--stealth`**: caps concurrency + adds random jitter between probes to soften the script-like traffic spike (slower, use when a provider is banning you for liveness-checking)
- Structured JSON reports for jq / PowerShell / CI

### 2. Model catalog `list-models` — fetch provider-declared model lists

- `GET /v1/models`, compatible with Anthropic / OpenAI response shapes
- Listed ≠ actually usable; it is only what the provider claims to support

### 3. Single-model deep diagnostics `inspect` — 7-dimension checkup

For a given `(provider, model)`, run text / streaming / metadata / context smoke / thinking / tool use / optional vision, and emit a unified JSON report:

| Dimension | What it checks |
|-----------|----------------|
| **text** | Real question + usage token parsing |
| **streaming** | SSE / non-SSE streaming, TTFT, event count, protocol type |
| **metadata** | `GET /v1/models/{id}` declared window / capabilities (labeled "not measured") |
| **context** | When undeclared, 512k / 1M char context smoke: accepted / rejected / timeout |
| **thinking** | Dual probe (disable vs enable): supports / forces / rejects |
| **tools** | Minimal side-effect-free tool: native / text_only / rejected |
| **vision** | Embedded 1×1 PNG, checks whether images are accepted (off by default) |
| **model-consistency** | Request model vs response model field, catches silent routing |
| **authenticity** | Model authenticity detection (see below; P0/P1 on by default, P2 probes opt-in) |

### 4. Model authenticity detection `authenticity` — is the relay swapping/downgrading the model?

The biggest hidden pit with relays: advertised as Claude Sonnet 4.5, but the backend is swapped to GPT / an older version / a quantized model / even a script fallback. The `authenticity` field of the `inspect` report uses 6 probes for probabilistic detection — **the verdict is a signal, not a cryptographic proof** (GhostPrint-style attacks can theoretically fool it).

| Probe | Default | Principle | Signal |
|-------|---------|-----------|--------|
| **Crosspack fields** `crosspack` | ✅ on | A native OpenAI response has only prompt/completion/total tokens; surfacing `cache_creation_input_tokens` / `usage_source: anthropic` means the relay forwarded an OpenAI request to a Claude backend then rewrapped. Zero new requests, reuses text/streaming raw body | Suspicious field → suspicious |
| **Thinking signature** `thinking_signature` | ✅ on | The `signature` on a Claude thinking block is an Anthropic server-side key signature; clients can only verify, not forge. A relay cannot fake it — it can only pass through real Claude output verbatim. Reuses thinking-enable response, zero new requests | thinking block present but no signature → suspicious |
| **Usage consistency** `usage_consistency` | ✅ on | A very short reply ("just OK") but a high `completion_tokens` → unexplainable billing. Reuses text_raw usage, zero new requests | Billing not self-consistent → suspicious |
| **Cache replay / temp-clamp** `cache_replay` | ❌ `--include cache-replay` | Send the same prompt twice at temp=1; byte-identical output → suspected cache replay or forced temperature clamp (a real model at temp=1 should vary). 2 new requests | Twin fire byte-identical → suspicious |
| **Knowledge cutoff** `knowledge_cutoff` | ❌ `--include knowledge-cutoff` | "before" questions (pre-2023 facts) every model should get right — a wrong answer is abnormal; "after" questions (2024-2025 events) only newer models should know. Distinguishes model version/generation. 6 new requests | before wrong → suspicious; after all wrong → note old model |
| **Distribution fingerprint** `js_fingerprint` | ❌ `--include js-fingerprint` | Ask N times "give a random number 1-100"; a real LLM is never uniform (has bias), only a script `random.randint` is near-uniform. JSD decides: closer to uniform than to the LLM-bias reference → suspected non-LLM backend. Default 50 (`--js-samples N`), <20 no verdict | Near-uniform distribution → suspicious |

```bash
# Default: crosspack + thinking_signature + usage_consistency (zero/low new requests, always on)
python check_ccswitch_health.py inspect --provider "Relay-A" --model "claude-sonnet-4-6" --human

# Full authenticity detection (burns ~58 extra requests, high confidence)
python check_ccswitch_health.py inspect --provider "Relay-A" --model "claude-sonnet-4-6" \
    --include text,streaming,metadata,thinking,tools,cache-replay,knowledge-cutoff,js-fingerprint \
    --js-samples 200 --human
```

The report's `authenticity.verdict` ∈ `clean` / `suspicious` / `inconclusive`, with `evidence` listing each suspicious proof. See [docs/PRD-authenticity-p0.md](docs/PRD-authenticity-p0.md) ~ `p2c.md`.

**Capability ceiling** (stated in the report, no exaggeration): 8-bit quantization / same-generation neighbor-tier downgrade (haiku vs sonnet) can't be detected with a few black-box requests; GhostPrint attack (arXiv:2606.16100) can theoretically fine-tune a weak model to fool the fingerprint; temperature=0 can only falsify one way (inconsistency → suspicious) but not prove authenticity.

---

## Quick start

### Requirements

- **Python 3.10+** (runtime uses the standard library only; no `pip install`)
- Windows / macOS / Linux
- [cc-switch](https://github.com/farion1231/cc-switch) installed and configured (default DB: `~/.cc-switch/cc-switch.db`)

> Entry barrier: a working local Python is required. Windows users who already use Claude Code / a dev environment usually already have Python. You can also double-click `run_health_check.ps1` (it auto-finds the interpreter via PATH / `CC_PULSE_PYTHON`).

### Install

```bash
git clone https://github.com/mengyuchun/cc-pulse.git
cd cc-pulse
# No pip install — stdlib only
python check_ccswitch_health.py check --help
```

### Preview

<p align="center">
  <img src="docs/screenshots/check.png" alt="check live progress" width="680"><br>
  <sub><code>check --failover-only</code> · multi-tier fallback + answer verification; HTTP 200 but empty/wrong answer → unusable</sub>
</p>

<p align="center">
  <img src="docs/screenshots/inspect.png" alt="inspect 7 dims + authenticity" width="680"><br>
  <sub><code>inspect --human</code> · 7-dimension diagnostics + 8th dimension "authenticity" (6 probes: thinking signature / crosspack fields / usage consistency / cache replay / knowledge cutoff / distribution fingerprint)</sub>
</p>

<p align="center">
  <img src="docs/screenshots/env-check.png" alt="env-check silent routing" width="680"><br>
  <sub><code>env-check</code> · detect env vars overriding cc-switch's choice (silent-routing root cause); exit code 2 on conflict</sub>
</p>

> Screenshots generated with the project's built-in mock HTTP server — no real keys or providers; data is for illustration only.

### Get started in 3 seconds

```bash
# Daily check: failover queue + current provider only (fastest)
python check_ccswitch_health.py check --failover-only
# With just installed, equivalent: just check

# Full check
python check_ccswitch_health.py check
# Equivalent: just check-all
```

> `Relay-A` / `Relay-B` / `claude-sonnet-4-6` / `glm-5` in the examples below are **placeholders** — replace them with your own provider names and model IDs as configured in cc-switch.

```bash
# JSON report (JSON on stdout, human progress on stderr)
python check_ccswitch_health.py check --failover-only --json | jq '.summary'

# Single-model deep diagnostics (human-readable)
python check_ccswitch_health.py inspect \
    --provider "Relay-A" --model "claude-sonnet-4-6" --human

# 1M context smoke + vision
python check_ccswitch_health.py inspect \
    --provider "Relay-A" --model "claude-sonnet-4-6" \
    --probe-context 1m --include text,streaming,metadata,thinking,tools,vision
```

> Windows users can also double-click `run_health_check.ps1` for an interactive menu — no need to memorize flags.

---

## Subcommands

### Scenario → command decision table

**Suddenly broken? Start with `just env-check`, then `just trend`** — the former checks whether env vars silently override cc-switch's choice; the latter surfaces cross-day degradation.

| What you want | Command | One-liner |
|---------------|---------|-----------|
| Which providers work right now | `just check` | Multi-tier probe + answer verification, see who really works |
| Fetch provider-declared models | `just models` | `GET /v1/models`; listed ≠ usable |
| Deep-dive one (provider, model) | `inspect` | 7-dimension checkup: text/streaming/metadata/context/thinking/tools/vision + routing match |
| Recent failures / success history | `history` / `just stats` | Read-only cc-switch DB, aggregated by provider / time window |
| Passive cc-switch health | `health` | Read `provider_health` only; no HTTP, not a replacement for active probing |
| Diagnose silent routing | `just env-check` / `routing` | env-check for env-var overrides; routing for request vs response model mismatch |
| Cross-day degradation trend | `just trend` | Read local probe archive, aggregate by day |
| Detect if the relay swaps/downgrades the model | `inspect --include ...,cache-replay,knowledge-cutoff,js-fingerprint` | 6-probe authenticity detection; P0/P1 on by default, P2 opt-in |
| Watch cc-switch logs live | `just watch` | Poll every 3s, prints new entries, Ctrl+C to stop |
| Multi-dimensional aggregation | `just analyze` | By day / model / provider×day matrix with sparklines |
| Batch deep-dive after check | `deep-dive` | Read check JSON, inspect each; CI-pipeable |
| Scheduled patrol + alert | `check --alert-threshold` | Warns when availability below threshold; cron-friendly |

### `check` — daily health check

Probes tiers in fallback order, **stops at the first successful tier**, and reports every attempt.

```bash
python check_ccswitch_health.py check --failover-only        # queue + current (recommended)
python check_ccswitch_health.py check                          # all claude providers
python check_ccswitch_health.py check --type all              # claude + codex + openclaw
python check_ccswitch_health.py check --failover-only --json  # machine-readable
```

**Live output example**:

```
Progress: print each tier as it finishes; print a provider summary when that provider is done

  · ProviderA            haiku  [401] 1.2s Invalid API key
  · ProviderB            haiku  [ok] 2.1s answer:"5"
[ 1/8] ✅ ProviderB               ✓haiku answer:"5"
  · ProviderA            sonnet [429] 1.6s Weekly limit reached
  · ProviderC            haiku  [wrong answer] 2.4s "..."
[ 2/8] ❌ ProviderA               haiku:401(...) | sonnet:429(...)
```

**Flags**:

| Flag | Meaning | Default |
|------|---------|---------|
| `--type claude\|codex\|openclaw\|all` | Provider type | `claude` |
| `--failover-only` | Only failover queue + current provider |
| `--current-only` | Only the active provider (narrowest; takes priority over `--failover-only` if both are set) | off |
| `--provider name/substr` | Filter by provider name (comma-separated names or substring) | off |
| `--select` | Interactive multi-provider selector (TTY only) | off |
| `--json` | Structured JSON on stdout; human text on stderr | off |
| `--workers N` | Concurrency | 6 |
| `--timeout SEC` | Per-request timeout seconds | 30 |
| `--probe-max-tokens N` | Probe token budget | 1024 |
| `--probe-enable-thinking` | Allow thinking mode | off |
| `--user-agent UA` | Override UA (default from local `claude --version`) | auto |
| `--stainless-version V` | Override `x-stainless-package-version` header | auto |
| `--stealth` | Stealth mode: cap concurrency (≤3) + per-request random delay, harder to flag as a liveness probe | off |
| `--skip-tls-verify` | ⚠️ Skip TLS certificate verification | off |

### `list-models` — fetch model catalogs

By default, only runs `GET /v1/models`; `--probe` performs a light text check per model; `--deep` performs five checks per model: text, streaming, metadata, thinking, and tools. In probe modes, `--source configured|listed|both` chooses models from configured tiers, the declared catalog, or their deduplicated union.

```bash
python check_ccswitch_health.py list-models
python check_ccswitch_health.py list-models --failover-only --type all
python check_ccswitch_health.py list-models --select --probe
python check_ccswitch_health.py list-models --failover-only --deep --source both --timeout 60
```

### Interactive provider selection: `--select`

`check` and `list-models` accept `--select` for a pure-stdlib, cross-platform keyboard multi-selector: arrow keys move, Space toggles, `a` selects/deselects all, Enter confirms, and Esc cancels. The CLI selector **does not support mouse input**. For `inspect`, specify the target provider with `--provider`.

It runs only when both stdin and stdout are TTYs. In pipelines and CI, it is skipped; use `--provider` to filter explicitly.

### `health` — passive health status

Reads cc-switch's `provider_health` table for health state, consecutive failures, and recent errors derived from real proxied traffic. It sends no HTTP requests and does not replace `check`'s active answer verification.

```bash
python check_ccswitch_health.py health
python check_ccswitch_health.py health --json
```

### `deep-dive` - batch deep-dive after check (CI-pipeable)

Reads check's JSON output, filters providers by `--target fail|ok|both`, deduplicates models, and inspects each. The CLI counterpart of the PS1 deep-dive flow, so CI / scripts can chain `check --json | deep-dive` without an interactive menu.

```bash
python check_ccswitch_health.py check --failover-only --json > check.json
python check_ccswitch_health.py deep-dive --from check.json --target fail
python check_ccswitch_health.py check --failover-only --json | \
  python check_ccswitch_health.py deep-dive --from - --target both --yes
python check_ccswitch_health.py deep-dive --from check.json --target both --json  # dry-run
```

| Flag | Description | Default |
|------|-------------|---------|
| `--from PATH\|-` | check JSON path, or `-` for stdin | required |
| `--target fail\|ok\|both` | Which providers to deep-dive | `fail` |
| `--models m1,m2` | Specific models (default: all deduped) | all |
| `--yes` | Skip confirmation when >20 combos | off |
| `--json` | Output task list JSON, no execution | off |

### `history` / `stats` / `routing` / `watch` — read-only cc-switch runtime logs

No HTTP: only reads `proxy_request_logs` in `~/.cc-switch/cc-switch.db` (optional on-disk log tail). Failures are highlighted with emoji + color badges (🔒AUTH / ⏳RATE / 📡NET / ❓MODEL / ⚠BAD / 💥5XX / ❌FAIL).

```bash
python check_ccswitch_health.py history
python check_ccswitch_health.py history --fails --limit 50
python check_ccswitch_health.py history --provider Fengwind --since 24h
python check_ccswitch_health.py stats --since 7d
python check_ccswitch_health.py routing --since 24h --limit 20
python check_ccswitch_health.py watch
python check_ccswitch_health.py watch --fails --provider Fengwind --interval 5
python check_ccswitch_health.py history --fails \
  --log-file ~/.cc-switch/logs/cc-switch.log --log-lines 80
```

| Flag | Meaning | Commands |
|------|---------|----------|
| `--limit N` | Row count | history / routing |
| `--fails` | Failures only | history |
| `--since 24h\|7d\|30m\|seconds` | Time window | history / stats / routing / analyze |
| `--provider substr` | Filter by name | history |
| `--json` | JSON output | all three |
| `--log-file PATH` | Tail on-disk log | history |
| `--with-history` | Attach 24h summary after check/inspect | check / inspect |
| `--history-since` | Time window used by `--with-history` | check / inspect |
| `--interval N` | Poll interval seconds (default 3) | watch |

Failures map into the same `error_category` enum used by live probes.

### `analyze` — multi-dimension aggregation analytics

No HTTP: reads `proxy_request_logs` for cross-dimensional aggregation. Includes ASCII sparkline trend charts.

```bash
# Full report (by day + by model + provider×day matrix)
python check_ccswitch_health.py analyze --since 7d

# Daily health trend only
python check_ccswitch_health.py analyze --mode day --since 30d

# Model latency percentiles (p50/p95/p99)
python check_ccswitch_health.py analyze --mode model --since 7d

# Provider × day success rate matrix
python check_ccswitch_health.py analyze --mode provider-day --since 14d

# Deep report for a single provider (day × model cross)
python check_ccswitch_health.py analyze --provider Fengwind --since 30d

# JSON output
python check_ccswitch_health.py analyze --since 7d --json
```

| Flag | Meaning | Commands |
|------|---------|----------|
| `--mode all\|day\|model\|provider-day\|provider` | Analysis dimension | analyze |
| `--provider substr` | Deep analysis for matched provider | analyze |
| `--since` | Time window | analyze |
| `--json` | JSON output | analyze |

### `env-check` — env override detection

Detects whether environment variables override the cc-switch-selected provider — the biggest real-world source of "silent routing" (e.g. a terminal `ANTHROPIC_BASE_URL`/`AUTH_TOKEN` silently beats the provider you selected in cc-switch). Reads env + config only, no HTTP.

```bash
# Human-readable
python check_ccswitch_health.py env-check

# JSON (findings + conflict count)
python check_ccswitch_health.py env-check --json
```

Exit code: **2** when conflicts exist (env would override the current provider), else 0. Secrets are masked (first 6 chars + `***`), never printed in full.

| Flag | Meaning |
|------|---------|
| `--json` | JSON output |

### `trend` — probe history trends

Each `check`/`inspect` probe appends one line to a local archive (default `~/.cc-pulse/probe_history.jsonl`, never touches cc-switch's database). `trend` reads that archive and aggregates success rate / latency percentiles / error categories / per-day trend per provider — surfacing degradation instead of a single snapshot. The archive auto-rotates when exceeding 5MB or 10000 records (`trim_archive`), keeping only the newest entries.

trend output includes a `trend_direction` marker: success rate compared across first and last days in the window — `↑` rising / `↓` falling / `→` stable.

```bash
# Last 7 days
python check_ccswitch_health.py trend --since 7d

# Single provider / model
python check_ccswitch_health.py trend --provider DeepSeek --since 30d

# Custom archive (paired with check --archive)
python check_ccswitch_health.py trend --archive ~/my_history.jsonl

# JSON output
python check_ccswitch_health.py trend --since 7d --json
```

`check`/`inspect` accept `--archive PATH` to override the archive path (per-project / per-machine isolation).

| Flag | Meaning | Default |
|------|---------|---------|
| `--since 24h\|7d\|30m\|seconds` | Time window | `7d` |
| `--archive PATH` | Archive file path | `~/.cc-pulse/probe_history.jsonl` |
| `--provider` | Restrict to one provider | all |
| `--model` | Restrict to one model | all |
| `--json` | JSON output | off |

### `inspect` — single-model deep diagnostics

```bash
# Default: text + streaming + routing + metadata + thinking + tools
python check_ccswitch_health.py inspect \
    --provider "Relay-A" --model "claude-sonnet-4-6"

# Human-readable output
python check_ccswitch_health.py inspect \
    --provider "Relay-A" --model "claude-sonnet-4-6" --human

# 1M context smoke (when no declared window)
python check_ccswitch_health.py inspect \
    --provider "Relay-A" --model "claude-sonnet-4-6" --probe-context 1m

# Explicitly enable vision
python check_ccswitch_health.py inspect \
    --provider "Relay-A" --model "claude-sonnet-4-6" \
    --include text,streaming,metadata,thinking,tools,vision

# Cross-provider compare (no --provider; defaults to text+streaming)
python check_ccswitch_health.py inspect \
    --compare "Relay-A/claude-sonnet-4-6,Relay-B/glm-5" --human
```

**Flags**:

| Flag | Meaning | Default |
|------|---------|---------|
| `--provider NAME` | Provider name (same as in cc-switch); optional with `--compare` | required for single |
| `--model ID` | Model ID (may include suffixes like `[1M]`) | required for single |
| `--compare A/m1,B/m2` | Cross-provider compare; targets carry provider | off |
| `--source configured\|listed\|manual` | Model source | `configured` |
| `--type claude\|codex\|openclaw\|all` | Limit provider type | `claude` |
| `--include LIST` | Checks to run (see table) | all on; `--compare` defaults to `text,streaming` |
| `--probe-context 512k\|1m` | Context smoke tier | `512k` |
| `--keep-suffix` | Keep `[1M]`-style suffixes in model ID | off |
| `--js-samples N` | `js-fingerprint` sample count (default 50; ≥200 high confidence, <20 no verdict) | `50` |
| `--ttft-timeout SEC` | Streaming first-token timeout | same as `--timeout` |
| `--with-metadata` | Backward-compatible; metadata is already on and sends no extra request | off |
| `--probe-delay SEC` | Delay between batch models | `3.0` |
| `--max-retries N` | Retry count for 429 | `1` |
| `--format human\|json` | Output format | `json` |
| `--human` | Human-readable output (default is JSON) | off |
| `--quiet` | Batch silent NDJSON + exit 0/3/4 | off |

**`--include` checks**:

| Item | Default | Meaning |
|------|---------|---------|
| `text` | ✅ | Real question + usage parsing |
| `streaming` | ✅ | SSE / non-SSE streaming, TTFT |
| `model-consistency` | ✅ | Request vs response model comparison |
| `protocol` / `error-classification` | ✅ | Protocol inference + error classification |
| `metadata` | ✅ | `GET /v1/models/{id}` declared values |
| `thinking` | ✅ | Dual probe: disable + enable |
| `tools` | ✅ | Minimal side-effect-free tool protocol probe |
| `vision` | ❌ | Embedded 1×1 PNG; enable with `--include ...,vision` |
| `cache-replay` | ❌ | temp=1 twin fire for cache replay/temp-clamp (2 extra requests) |
| `knowledge-cutoff` | ❌ | before/after bank for model version/generation (6 extra requests) |
| `js-fingerprint` | ❌ | Single-token random-number distribution fingerprint JSD (default `--js-samples` 50 extra requests) |

**`--source` values**:

| Value | Behavior |
|-------|----------|
| `configured` | Exact match against tiers configured in cc-switch (no network) |
| `listed` | First `GET /v1/models`, then look up in the returned list |
| `manual` | Force the literal `--model` value (advanced) |

> ⏱ **Total timeout note**: default `inspect` issues 5–6 serial requests, so worst-case wall time ≈ N × `--timeout`. For example, `--timeout 30` can take up to ~180s. Use `--include text` for a faster single-check run.

---

## Output examples

### Human-readable (`--human`)

```
============================================================
  Provider:  Relay-A
  Model:     claude-sonnet-4-6 (configured)
  Protocol:  anthropic_messages · confirmed
============================================================

[1/7] Text probe
  Status: ✅ pass · 1.24s
  Answer: "5" · correct
  usage: in=20 out=3

[2/7] Streaming probe
  Status: ✅ pass · TTFT 0.42s · total 1.31s

[3/7] Model routing
  Match: exact_match

[4/7] Model metadata
  Declared context window: 200,000 tokens (provider-declared, not measured)

[5/7] Thinking
  verdict: supports_disable

[6/7] Tool use
  Status: ✅ pass · support=native

[7/7] Authenticity
  verdict: clean
  · Crosspack: no anomaly (no Anthropic-only fields in OpenAI response)
  · Thinking signature: valid (genuine Claude server-side output)
  · Usage consistency: completion_tokens matches answer length

------------------------------------------------------------
  Summary: healthy
============================================================
```

### JSON report fields

| Field | Meaning |
|-------|---------|
| `protocol.detected` | `anthropic_messages` / `openai_responses` / `openai_chat_completions` / `unknown` |
| `protocol.confidence` | `inferred` / `confirmed` (upgraded after successful text probe) |
| `text.status` | `pass` / `fail` / `error` |
| `text.answer` / `text.correct` | Extracted answer / equals `"5"` |
| `streaming.ttft_seconds` | Time to first token (seconds) |
| `streaming.response_model` / `event_count` / `is_sse` | Response model / event count / true SSE |
| `metadata.declared_context_window` | Provider-**declared** window (not measured) |
| `metadata.capabilities` | `{"image_input": true, "thinking": true, ...}` |
| `context.status` | `accepted` / `rejected` / `timeout` / `error` / `skipped` |
| `context.approx_input_chars` / `token_estimate` | Smoke payload size and upper-bound note |
| `thinking.verdict` | `supports_disable` / `forces_thinking` / `rejects_thinking_field` / `breaks_on_short_budget` |
| `tools.protocol_support` | `native` / `text_only` / `rejected` / `unknown` |
| `vision.status` | `pass` / `fail` / `error` / `skipped` / `unsupported` |
| `usage.present` / `input_tokens` / `output_tokens` | Whether real token counts were parsed |
| `model_consistency.match` | `exact_match` / `alias_match` / `fuzzy_match` / `mismatch` / `unverifiable` |
| `authenticity.verdict` | `clean` / `suspicious` / `inconclusive` (authenticity summary) |
| `authenticity.crosspack` / `thinking_signature` / `usage_consistency` | P0/P1 probes on by default (zero/low new requests) |
| `authenticity.cache_replay` / `knowledge_cutoff` / `js_fingerprint` | P2 probes, opt-in via `--include` |
| `authenticity.evidence` | List of suspicious proofs (human-readable) |
| `summary.verdict` | `healthy` / `available_but_wrong_answer` / `unavailable` / `skipped` |
| `summary.recommended_actions` | Actionable suggestions based on results |

### Error category enum (`error_category`)

Each probe result's `error_category` is one of:

```
none | network | tls | authentication | rate_limit | model_not_found |
protocol_incompatible | server_error | invalid_response | answer_mismatch |
stream_protocol | ttft_timeout | stream_incomplete | unknown
```

---

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success (`check` has at least one usable provider / `inspect` healthy or skipped / `list-models` finished) |
| 1 | Partial failure (`check` found unusable providers / `inspect` some models failed / wrong answer) |
| 2 | Environment / parameter error (DB missing, no matching providers, resolve failed) |
| 3 | All failed (`inspect` all models failed) |
| 4 | User cancelled (PS1 launcher ESC) |

> Batch/compare mode uses 1/3 for finer granularity so CI and `&&` chains can branch. Pair with `--quiet` for pure NDJSON output — one JSON object per model on stdout, all progress messages silenced.

---

## Windows desktop launcher

`run_health_check.ps1` provides an interactive menu — double-click, no flags required (PowerShell 7+):

```
[1] Health check          quick / custom, deep-dive after check
[2] Deep diagnostics      list models / inspect
[3] Runtime logs          fails / stats / routing / watch
[4] Advanced settings     JSON / stealth / thinking / UA
[5] Exit
```

### Menu paths

| Entry | Hierarchy and behavior |
|-------|------------------------|
| Quick health check | Directly uses type and scope from Advanced settings; defaults to `claude` + failover queue/current provider |
| Custom health check | Type → queue+current / all / current only / selected providers (multi-select) |
| List models | Type → scope → catalog only / light probe / deep probe; probe modes then choose `listed/configured/both` source |
| Deep diagnostics | Type → providers; multi-provider mode selects tiers and checks every provider×tier model, while one-provider mode offers one model, all models, or selected models and dimensions |
| Runtime logs | Recent failures, all logs, statistics, silent routing, live watch, and analysis |
| Advanced settings | JSON, token budget, thinking, UA, inspect context/vision, authenticity probes (cache-replay/knowledge-cutoff/js-fingerprint), check stealth, plus quick-check type/scope; valid only for this launcher process and reset when the window closes |

### Interaction

Interactive terminals support arrow keys/mouse wheel to move, Enter to confirm, and Esc or right-click to cancel. Multi-select additionally uses Space to toggle and `a` to select/deselect all; left-click selects or toggles. With redirected input, ordinary menus and lists use compatible numbered or text input instead, but inspect's multi-provider tier and custom-dimension branches require an interactive console. Do not treat the launcher menu as an unattended automation interface.

- Prefer interpreter from `CC_PULSE_PYTHON`, then `python` on PATH
- DB path override: `CC_PULSE_DB`
- Timeout override: `CC_PULSE_TIMEOUT` (seconds; health-check default)
- Uses `python -u` for unbuffered output so progress is live

### Environment variables

| Variable | Purpose |
|----------|---------|
| `CC_PULSE_PYTHON` | Preferred Python interpreter for the launcher |
| `CC_PULSE_DB` | Default cc-switch.db path for the launcher |
| `CC_PULSE_TIMEOUT` | Default timeout seconds for health checks |
| `CC_PULSE_PWSH` | pwsh path used by tests |

`CC_PULSE_*` variables configure the launcher. Provider routing and authentication variables (such as `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`) are audited by `env-check`; they are not the same variables.

---

## Design principles (intentional, not bugs)

- **Read-only, zero intrusion**: open the DB with `file:...?mode=ro`; never modify cc-switch
- **No path de-duplication**: always `base_url + /v1/messages`; a base ending with `/v1` becomes `/v1/v1/messages` — deliberately matching real Claude Code behavior
- **Full error text passthrough**: JSON error `message` is not truncated; HTML / non-JSON shows the first 500 chars plus true length
- **Local archive; never modify cc-switch data**: the DB always opens with `file:...?mode=ro`; `check`/`inspect` append CC-Pulse's own `~/.cc-pulse/probe_history.jsonl` for `trend`, and `--archive PATH` overrides its location. The cc-switch database is never written.
- **Claude Code fingerprint headers**: UA from local `claude --version` (overridable via `--user-agent`; `x-stainless-*` version overridable via `--stainless-version`) to reduce Cloudflare 1010 false rejects
- **Anti-detection by default**: the probe question is drawn at random from a pool (avoids fixed-prompt substring matching), `max_tokens` defaults to a natural `1024` (not the tell-tale `20`), and thinking-suppression fields are sent only to thinking-prone models (deepseek/glm etc.) — regular claude requests stay closer to real claude-cli
- **`--stealth` timing camouflage**: caps concurrency at 3 and adds a random 0.3–1.5s delay before each probe to flatten script-like traffic spikes — off by default (slower); turn it on when a provider starts banning health-check traffic
- **TLS verified by default**: `--skip-tls-verify` must be explicit (exposes credentials if abused)
- **Terminal safety**: `say()` strips ANSI escapes and control characters to reduce malicious provider response injection

## Honest limitations

- `check` mainly verifies "can it answer a simple arithmetic question"; `inspect` adds streaming / metadata / context / thinking / tools / vision, but not multi-turn chat or sustained concurrency capacity
- `metadata.declared_context_window` is the provider's **claimed** value; undeclared context smoke uses a **1 char ≈ 1 token upper bound**, not a precise tokenizer count
- Claude Code fingerprint headers are not 100% complete; some strict validators may still reject the client
- Models from `list-models` ≠ actually usable; they are only declared support lists
- `inspect` never auto-runs cc-switch failover; it only emits **read-only diagnostics**
- Anti-detection lowers but cannot eliminate the odds of being flagged: any "verify a correct answer" probe needs a checkable prompt, which is inherently a low-frequency, fixed-shape pattern in real traffic — a structural trade-off, not a bug
- Authenticity detection is a **probabilistic signal**: 8-bit quantization / same-generation neighbor-tier downgrade is not detectable; GhostPrint-style attacks can theoretically fool the fingerprint; it does not promise "definitive proof of genuineness"

## Known scenarios and responses

| Scenario | Symptom | Response |
|----------|---------|----------|
| Thinking model burns tokens | 200 with empty answer | auto-suppressed for known thinking models; or raise `--probe-max-tokens` / use `--probe-enable-thinking` |
| Provider bans probe scripts | Blocked / disabled after a scan | `--stealth` (concurrency ≤3 + random jitter); the question pool + natural `max_tokens` are always on |
| Provider validates client UA | 403 `client_restricted` | `--user-agent "codex_cli_rs/0.50.0"` etc., or `--stainless-version` |
| Provider only accepts x-api-key | 401 `Missing API key` | Use `ANTHROPIC_API_KEY` in cc-switch |
| Silent model routing | Request / response model mismatch | `inspect` `model_consistency` marks `mismatch` |
| OAuth token in wrong field | 401 `invalid x-api-key` | Use `ANTHROPIC_AUTH_TOKEN` (Bearer), not `ANTHROPIC_API_KEY` |
| Shrinking context | Claims 1M, rejects around 526k | `inspect` context smoke marks `rejected` |

---

## Tests

```bash
# Run all tests (Python core + PS1 launcher)
just test && just test-ps1

# Python core only (unit + end-to-end mocks)
just test

# PS1 launcher end-to-end (requires pwsh)
just test-ps1

# Docs consistency guard (prevents README drift)
just lint-docs
```

Tests use the standard library only, with an embedded mock HTTP server, and never hit real providers. Currently **612 Python tests + 47 PS1 tests**, distributed by file:

| Test file | Purpose | Cases |
|-----------|---------|-------|
| `tests/test_ccpulse_full.py` | Unit + end-to-end (mock SSE / multi-protocol / multi-type) | 398 |
| `tests/test_archive_trend.py` | Probe archive & trend aggregation | 54 |
| `tests/test_authenticity.py` | P0 crosspack fields + thinking signature | 14 |
| `tests/test_usage_consistency.py` | P1 usage consistency static check | 9 |
| `tests/test_cache_replay.py` | P2-A cache replay / temp-clamp twin fire | 8 |
| `tests/test_knowledge_cutoff.py` | P2-B knowledge cutoff before/after bank | 9 |
| `tests/test_js_fingerprint.py` | P2-C single-token distribution fingerprint JSD | 10 |
| `tests/test_p0_protocol_fix.py` | P0 protocol-inference fix regression | 2 |
| `tests/test_env_check.py` | Env var override detection | 14 |
| `tests/test_tui.py` | TUI arrow/mouse selector | 9 |
| `tests/test_ps1_launcher.py` | PS1 launcher interaction flow | 47 |
| `tests/test_docs_consistency.py` | Docs ↔ code consistency guard | — |

### `just` command cheat sheet

Confirmed against `justfile`; available once [just](https://just.systems/) is installed:

| Command | Equivalent to | Purpose |
|---------|---------------|---------|
| `just check` | `check --failover-only --workers 8 --timeout 45` | Daily check (fastest) |
| `just check-all` | `check --workers 8 --timeout 45` | Full check |
| `just check-stealth` | `check --failover-only --stealth` | Stealth mode (when flagged) |
| `just models` | `list-models --failover-only` | Fetch queue models |
| `just models-probe` | `list-models --failover-only --probe` | Fetch + light probe |
| `just models-deep` | `list-models --failover-only --deep` | Fetch + five-check deep probe |
| `just trend` | `trend --since 7d` | 7-day probe trend |
| `just env-check` | `env-check` | Env var override detection |
| `just stats` | `stats --since 7d` | 7-day stats |
| `just routing` | `routing --since 7d --limit 20` | Silent routing ranking |
| `just watch` | `watch --interval 3` | Live monitoring |
| `just analyze` | `analyze --since 7d` | Full-dimension analysis |
| `just test` | `python tests/test_ccpulse_full.py` | Python tests |
| `just test-ps1` | `python tests/test_ps1_launcher.py` | PS1 launcher tests |
| `just test-all` | runs all 7 test files | Full test suite |
| `just lint-docs` | `python tests/test_docs_consistency.py` | Docs consistency guard |
| `just format` / `just lint` | ruff format / ruff check | Dev formatting & lint |

> No just? The raw `python check_ccswitch_health.py ...` forms are all shown in the subcommand sections above.

## Development

```bash
# Format + lint
just format
just lint
```

Uses [ruff](https://github.com/astral-sh/ruff) for formatting and linting (dev-time only; zero runtime deps).

---

## Project layout

```
CC-Pulse/
├── check_ccswitch_health.py   # Main CLI: argparse command orchestration, provider parsing, reports, log analytics
├── ccpulse_net.py             # Network layer: HTTP requests, SSE parsing, error classification, TLS, body sanitization
├── ccpulse_probe.py           # Probe layer: probe_tier/probe_stream, protocol payloads, inspect 7 dims, authenticity 6 probes
├── ccpulse_store.py           # Storage layer: read-only cc-switch SQLite access, provider/tier parsing, log queries
├── ccpulse_archive.py         # Archive layer: local probe_history.jsonl read/write, rotation, trend aggregation
├── ccpulse_env.py             # Env layer: env-var override detection (silent routing)
├── ccpulse_output.py          # Output layer: ContextVar routing, terminal sanitization, ANSI styles
├── ccpulse_tui.py             # TUI layer: cross-platform arrow/mouse multi-select
├── run_health_check.ps1       # Windows desktop interactive menu launcher (PowerShell 7+)
├── justfile                   # Common tasks (check, format, lint, test)
├── requirements.txt           # Declares: stdlib only, no runtime deps
├── tests/                     # See the test table above
├── docs/
│   ├── CODEMAPS/              # Repo architecture maps (architecture / backend / data / dependencies)
│   ├── PRD-authenticity-p0.md ~ p2c.md   # Authenticity 6-probe PRD + TDD acceptance
│   ├── product-research.md    # Internal competitive-research notes
│   └── README.md              # docs/ index
├── CLAUDE.md                  # Project-level Claude Code instructions
├── LICENSE                    # MIT License
├── README.md                  # Chinese docs
└── README.en.md               # English docs
```

### Layered architecture

```
┌──────────────────────────────────────────────────────────┐
│  CLI / PS1 launcher                                       │
│  check_ccswitch_health.py (argparse) / run_health_check.ps1│
└────────────┬─────────────────────────────────┬───────────┘
             │ command dispatch                 │ interactive menu
             ▼                                 ▼
┌──────────────────────────┐   ┌───────────────────────────┐
│ ccpulse_store (read-only SQLite)│   │ ccpulse_tui (selectors)    │
│ provider / tier / log queries   │   │ ccpulse_output (routing)   │
└─────────────┬────────────┘   └───────────────────────────┘
              │ Provider + ModelTier
              ▼
┌──────────────────────────────────────────────────────────┐
│  ccpulse_probe (probe layer)                              │
│  probe_tier / probe_stream / inspect 7 dimensions        │
│  authenticity: crosspack / thinking_signature / usage_c  │
│           cache_replay / knowledge_cutoff / js_fingerprint│
└────────────┬─────────────────────────────────┬───────────┘
             │ HTTP                            │ archive
             ▼                                 ▼
┌──────────────────────────┐   ┌───────────────────────────┐
│ ccpulse_net (network)    │   │ ccpulse_archive (trend)    │
│ HTTP / SSE / error class │   │ probe_history.jsonl        │
│ TLS / body sanitization  │   └───────────────────────────┘
└──────────────────────────┘
```

Four clear layers: `store` parses config read-only → `probe` sends requests and diagnoses → `net` handles transport & sanitization → `archive` persists local trends. `check_ccswitch_health.py` is the single command-orchestration entry; layers are re-exported via `__getattr__` for importlib-based tests.

---

## Related projects

| Project | Form | Comparison |
|---------|------|------------|
| [all-api-hub](https://github.com/qixing-jk/all-api-hub) | Browser extension | Most features, Cloudflare handling; does not read the cc-switch DB |
| [cc-test](https://github.com/zhoujun681/cc-test) | Rust CLI | Similar goal, but no multi-tier fallback or answer verification |
| [cc-switcher](https://github.com/jimstratus/cc-switcher) | PowerShell | Switching-first, probing secondary |

CC-Pulse's trade-off: **small and focused** — deep probing for cc-switch providers only (multi-tier fallback + answer verification + config-driven auth + 7-dimension single-model diagnostics + authenticity detection). No provider management UI, no switching UI.

---

## Technical roadmap

### Design iron rules (intentional, unbreakable)

- **Zero runtime third-party deps**: pure Python standard library (`argparse`/`sqlite3`/`urllib`/`ssl`/`threading`/`concurrent.futures`/`contextvars`/`math`/`re`); no click/typer/rich/aiohttp
- **API key never leaves the machine**: the key is used only in request headers, never written to logs/reports/archives; response bodies are sanitized via `_sanitize_raw_body` before reaching `raw_body`
- **Read-only cc-switch SQLite**: opened with `file:...?mode=ro`, never modified; trend data goes to CC-Pulse's own `~/.cc-pulse/probe_history.jsonl`
- **Never auto-switches providers**: emits read-only diagnostics and switch instructions only; never decides for the user
- **No web UI / HTML report**: CLI + JSON/NDJSON is the only product form

### Authenticity detection roadmap (P0 → P2-C all shipped)

Sorted by cost-effectiveness — deterministic, key-stays-local, zero-dependency judgments first:

| Stage | Probe | Judgment type | Request cost | Status |
|-------|-------|---------------|--------------|--------|
| **P0** | thinking signature | cryptographic (unforgeable) | 0 (reuses thinking response) | ✅ shipped |
| **P0** | crosspack field detection | field-level JSON parsing | 0 (reuses text/streaming raw body) | ✅ shipped |
| **P1** | usage consistency static check | billing self-consistency | 0 (reuses text_raw usage) | ✅ shipped |
| **P2-A** | cache replay / temp-clamp twin fire | temp=1 byte-identical compare | 2 requests | ✅ shipped |
| **P2-B** | knowledge cutoff before/after bank | factual-knowledge generation split | 6 requests | ✅ shipped |
| **P2-C** | single-token distribution fingerprint JSD | distribution divergence (paper: *One Token Is Enough*) | 50 (tunable) | ✅ shipped |

**Design trade-offs**:
- **Self-contained judgments first**: P2-C uses a programmatically constructed `LLM_BIAS_REFERENCE` (preferred numbers ×3 / round numbers ×0.3, normalized) + uniform-distribution dual-hypothesis JSD — no per-vendor fingerprint library (avoids licensing & maintenance)
- **Deterministic > probabilistic**: P0 thinking signature is cryptographic proof (relays cannot forge), so it ranks highest; P2 distribution fingerprint is a probabilistic signal, so it goes last
- **Zero new requests first**: P0/P1 reuse existing response bodies at zero extra cost; P2 is where requests start burning
- **Capability ceiling is explicit**: 8-bit quantization / same-generation neighbor tiers / GhostPrint attacks are acknowledged limits, stated in the report without exaggeration

### Non-goals (YAGNI)

- No logprobs-based methods (most relays don't expose logprobs)
- No methods requiring a local real model (RUT/IRIS/perplexity need torch/GPU — violates zero-dep)
- No "definitive proof of genuineness" — only probabilistic signals + evidence
- No web/HTML report product
- No provider management / switching / UI (that's cc-switch's domain)

See [docs/PRD-authenticity-p0.md](docs/PRD-authenticity-p0.md) ~ [p2c.md](docs/PRD-authenticity-p2c.md) for details.

---

## Contributing

Issues and PRs are welcome. Please ensure:

1. `just test` is green
2. `just lint` introduces no new warnings
3. New features include matching tests
4. Existing style is followed (`ruff format`)

## License

[MIT License](LICENSE) © 2026 Yuchun Meng
